from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field
import datetime
import os
import traceback
from typing import (
    Dict,
    Optional,
    Mapping,
    Callable,
    Any,
    List,
    Type,
    Union,
    Tuple,
    Set,
    MutableMapping,
)
from itertools import chain
import time
from dbt.events.base_types import EventLevel
import json
import pprint
import msgpack

import dbt.exceptions
import dbt.tracking
import dbt.utils
from dbt.flags import get_flags

from dbt.adapters.factory import (
    get_adapter,
    get_relation_class_by_name,
    get_adapter_package_names,
)
from dbt.constants import (
    DEPENDENCIES_FILE_NAME,
    MANIFEST_FILE_NAME,
    PARTIAL_PARSE_FILE_NAME,
    SEMANTIC_MANIFEST_FILE_NAME,
)
from dbt.helper_types import PathSet
from dbt.clients.yaml_helper import load_yaml_text
from dbt.events.functions import fire_event, get_invocation_id, warn_or_error
from dbt.events.helpers import datetime_to_json_string
from dbt.events.types import (
    PartialParsingErrorProcessingFile,
    PartialParsingError,
    ParsePerfInfoPath,
    PartialParsingSkipParsing,
    UnableToPartialParse,
    PartialParsingNotEnabled,
    ParsedFileLoadFailed,
    InvalidDisabledTargetInTestNode,
    NodeNotFoundOrDisabled,
    StateCheckVarsHash,
    Note,
    PublicationArtifactChanged,
    PublicationArtifactAvailable,
    DeprecatedModel,
    DeprecatedReference,
    UpcomingReferenceDeprecation,
)
from dbt_extractor import py_extract_from_source  # type: ignore
from dbt.logger import DbtProcessState
from dbt.node_types import NodeType, AccessType
from dbt.clients.jinja import get_rendered, MacroStack
from dbt.clients.jinja_static import statically_extract_macro_calls
from dbt.clients.system import (
    make_directory,
    path_exists,
    read_json,
    write_file,
    resolve_path_from_base,
    load_file_contents,
)
from dbt.config import Project, RuntimeConfig
from dbt.context.docs import generate_runtime_docs_context
from dbt.context.macro_resolver import MacroResolver, TestMacroNamespace
from dbt.context.configured import generate_macro_context
from dbt.context.providers import ParseProvider
from dbt.contracts.files import FileHash, ParseFileType, SchemaSourceFile
from dbt.parser.read_files import (
    ReadFilesFromFileSystem,
    load_source_file,
    FileDiff,
    ReadFilesFromDiff,
)
from dbt.parser.partial import PartialParsing, special_override_macros
from dbt.contracts.graph.manifest import (
    Manifest,
    Disabled,
    MacroManifest,
    ManifestStateCheck,
    ParsingInfo,
)
from dbt.graph.graph import UniqueId
from dbt.contracts.graph.nodes import (
    SourceDefinition,
    Macro,
    Exposure,
    Metric,
    SeedNode,
    ManifestNode,
    ResultNode,
    ModelNode,
    NodeRelation,
)
from dbt.contracts.graph.unparsed import NodeVersion
from dbt.contracts.util import Writable
from dbt.contracts.publication import (
    PublicationConfig,
    PublicationArtifact,
    PublicationMetadata,
    PublicModel,
    ProjectDependencies,
)
from dbt.exceptions import (
    TargetNotFoundError,
    AmbiguousAliasError,
    PublicationConfigNotFound,
    ProjectDependencyCycleError,
    InvalidAccessTypeError,
)
from dbt.parser.base import Parser
from dbt.parser.analysis import AnalysisParser
from dbt.parser.generic_test import GenericTestParser
from dbt.parser.singular_test import SingularTestParser
from dbt.parser.docs import DocumentationParser
from dbt.parser.hooks import HookParser
from dbt.parser.macros import MacroParser
from dbt.parser.models import ModelParser
from dbt.parser.schemas import SchemaParser
from dbt.parser.search import FileBlock
from dbt.parser.seeds import SeedParser
from dbt.parser.snapshots import SnapshotParser
from dbt.parser.sources import SourcePatcher
from dbt.version import __version__

from dbt.dataclass_schema import StrEnum, dbtClassMixin

PARSING_STATE = DbtProcessState("parsing")
PERF_INFO_FILE_NAME = "perf_info.json"


def extended_mashumaro_encoder(data):
    return msgpack.packb(data, default=extended_msgpack_encoder, use_bin_type=True)


def extended_msgpack_encoder(obj):
    if type(obj) is datetime.date:
        date_bytes = msgpack.ExtType(1, obj.isoformat().encode())
        return date_bytes
    elif type(obj) is datetime.datetime:
        datetime_bytes = msgpack.ExtType(2, obj.isoformat().encode())
        return datetime_bytes

    return obj


def extended_mashumuro_decoder(data):
    return msgpack.unpackb(data, ext_hook=extended_msgpack_decoder, raw=False)


def extended_msgpack_decoder(code, data):
    if code == 1:
        d = datetime.date.fromisoformat(data.decode())
        return d
    elif code == 2:
        dt = datetime.datetime.fromisoformat(data.decode())
        return dt
    else:
        return msgpack.ExtType(code, data)


def version_to_str(version: Optional[Union[str, int]]) -> str:
    if isinstance(version, int):
        return str(version)
    elif isinstance(version, str):
        return version

    return ""


class ReparseReason(StrEnum):
    version_mismatch = "01_version_mismatch"
    file_not_found = "02_file_not_found"
    vars_changed = "03_vars_changed"
    profile_changed = "04_profile_changed"
    deps_changed = "05_deps_changed"
    project_config_changed = "06_project_config_changed"
    load_file_failure = "07_load_file_failure"
    exception = "08_exception"
    proj_env_vars_changed = "09_project_env_vars_changed"
    prof_env_vars_changed = "10_profile_env_vars_changed"


# Part of saved performance info
@dataclass
class ParserInfo(dbtClassMixin):
    parser: str
    elapsed: float
    parsed_path_count: int = 0


# Part of saved performance info
@dataclass
class ProjectLoaderInfo(dbtClassMixin):
    project_name: str
    elapsed: float
    parsers: List[ParserInfo] = field(default_factory=list)
    parsed_path_count: int = 0


# Part of saved performance info
@dataclass
class ManifestLoaderInfo(dbtClassMixin, Writable):
    path_count: int = 0
    parsed_path_count: int = 0
    static_analysis_path_count: int = 0
    static_analysis_parsed_path_count: int = 0
    is_partial_parse_enabled: Optional[bool] = None
    is_static_analysis_enabled: Optional[bool] = None
    read_files_elapsed: Optional[float] = None
    load_macros_elapsed: Optional[float] = None
    parse_project_elapsed: Optional[float] = None
    patch_sources_elapsed: Optional[float] = None
    process_manifest_elapsed: Optional[float] = None
    load_all_elapsed: Optional[float] = None
    projects: List[ProjectLoaderInfo] = field(default_factory=list)
    _project_index: Dict[str, ProjectLoaderInfo] = field(default_factory=dict)

    def __post_serialize__(self, dct):
        del dct["_project_index"]
        return dct


# The ManifestLoader loads the manifest. The standard way to use the
# ManifestLoader is using the 'get_full_manifest' class method, but
# many tests use abbreviated processes.
class ManifestLoader:
    def __init__(
        self,
        root_project: RuntimeConfig,
        all_projects: Mapping[str, Project],
        macro_hook: Optional[Callable[[Manifest], Any]] = None,
        file_diff: Optional[FileDiff] = None,
        publications: Optional[List[PublicationArtifact]] = None,
    ) -> None:
        self.root_project: RuntimeConfig = root_project
        self.all_projects: Mapping[str, Project] = all_projects
        self.file_diff = file_diff
        self.publications: Mapping[str, PublicationArtifact] = (
            {publication.project_name: publication for publication in publications}
            if publications
            else {}
        )
        self.manifest: Manifest = Manifest()
        self.new_manifest = self.manifest
        self.manifest.metadata = root_project.get_metadata()
        self.macro_resolver = None  # built after macros are loaded
        self.started_at = time.time()
        # This is a MacroQueryStringSetter callable, which is called
        # later after we set the MacroManifest in the adapter. It sets
        # up the query headers.
        self.macro_hook: Callable[[Manifest], Any]
        if macro_hook is None:
            self.macro_hook = lambda m: None
        else:
            self.macro_hook = macro_hook

        self._perf_info = self.build_perf_info()

        # State check determines whether the saved_manifest and the current
        # manifest match well enough to do partial parsing
        self.manifest.state_check = self.build_manifest_state_check()
        # We need to know if we're actually partially parsing. It could
        # have been enabled, but not happening because of some issue.
        self.partially_parsing = False
        self.partial_parser = None

        # This is a saved manifest from a previous run that's used for partial parsing
        self.saved_manifest: Optional[Manifest] = self.read_manifest_for_partial_parse()

    # This is the method that builds a complete manifest. We sometimes
    # use an abbreviated process in tests.
    @classmethod
    def get_full_manifest(
        cls,
        config: RuntimeConfig,
        *,
        file_diff: Optional[FileDiff] = None,
        reset: bool = False,
        write_perf_info=False,
        publications: Optional[List[PublicationArtifact]] = None,
    ) -> Manifest:

        adapter = get_adapter(config)  # type: ignore
        # reset is set in a TaskManager load_manifest call, since
        # the config and adapter may be persistent.
        if reset:
            config.clear_dependencies()
            adapter.clear_macro_manifest()
        macro_hook = adapter.connections.set_query_header

        # Hack to test file_diffs
        if os.environ.get("DBT_PP_FILE_DIFF_TEST"):
            file_diff_path = "file_diff.json"
            if path_exists(file_diff_path):
                file_diff_dct = read_json(file_diff_path)
                file_diff = FileDiff.from_dict(file_diff_dct)

        with PARSING_STATE:  # set up logbook.Processor for parsing
            # Start performance counting
            start_load_all = time.perf_counter()

            projects = config.load_dependencies()
            loader = cls(
                config,
                projects,
                macro_hook=macro_hook,
                file_diff=file_diff,
                publications=publications,
            )

            manifest = loader.load()

            _check_manifest(manifest, config)
            manifest.build_flat_graph()

            # This needs to happen after loading from a partial parse,
            # so that the adapter has the query headers from the macro_hook.
            loader.save_macros_to_adapter(adapter)

            # Save performance info
            loader._perf_info.load_all_elapsed = time.perf_counter() - start_load_all
            loader.track_project_load()

            if write_perf_info:
                loader.write_perf_info(config.project_target_path)

        return manifest

    # This is where the main action happens
    def load(self):
        start_read_files = time.perf_counter()

        # This updates the "files" dictionary in self.manifest, and creates
        # the partial_parser_files dictionary (see read_files.py),
        # which is a dictionary of projects to a dictionary
        # of parsers to lists of file strings. The file strings are
        # used to get the SourceFiles from the manifest files.
        saved_files = self.saved_manifest.files if self.saved_manifest else {}
        if self.file_diff:
            # We're getting files from a file diff
            file_reader = ReadFilesFromDiff(
                all_projects=self.all_projects,
                files=self.manifest.files,
                saved_files=saved_files,
                root_project_name=self.root_project.project_name,
                file_diff=self.file_diff,
            )
        else:
            # We're getting files from the file system
            file_reader = ReadFilesFromFileSystem(
                all_projects=self.all_projects,
                files=self.manifest.files,
                saved_files=saved_files,
            )

        # Set the files in the manifest and save the project_parser_files
        file_reader.read_files()
        self.manifest.files = file_reader.files
        project_parser_files = orig_project_parser_files = file_reader.project_parser_files
        self._perf_info.path_count = len(self.manifest.files)
        self._perf_info.read_files_elapsed = time.perf_counter() - start_read_files

        skip_parsing = False
        if self.saved_manifest is not None:
            self.partial_parser = PartialParsing(self.saved_manifest, self.manifest.files)
            skip_parsing = self.partial_parser.skip_parsing()
            if skip_parsing:
                # nothing changed, so we don't need to generate project_parser_files
                self.manifest = self.saved_manifest
            else:
                # create child_map and parent_map
                self.saved_manifest.build_parent_and_child_maps()
                # create group_map
                self.saved_manifest.build_group_map()
                # files are different, we need to create a new set of
                # project_parser_files.
                try:
                    project_parser_files = self.partial_parser.get_parsing_files()
                    self.partially_parsing = True
                    self.manifest = self.saved_manifest
                except Exception as exc:
                    # pp_files should still be the full set and manifest is new manifest,
                    # since get_parsing_files failed
                    fire_event(
                        UnableToPartialParse(
                            reason="an error occurred. Switching to full reparse."
                        )
                    )

                    # Get traceback info
                    tb_info = traceback.format_exc()
                    formatted_lines = tb_info.splitlines()
                    (_, line, method) = formatted_lines[-3].split(", ")
                    exc_info = {
                        "traceback": tb_info,
                        "exception": formatted_lines[-1],
                        "code": formatted_lines[-2],
                        "location": f"{line} {method}",
                    }

                    # get file info for local logs
                    parse_file_type = None
                    file_id = self.partial_parser.processing_file
                    if file_id:
                        source_file = None
                        if file_id in self.saved_manifest.files:
                            source_file = self.saved_manifest.files[file_id]
                        elif file_id in self.manifest.files:
                            source_file = self.manifest.files[file_id]
                        if source_file:
                            parse_file_type = source_file.parse_file_type
                            fire_event(PartialParsingErrorProcessingFile(file=file_id))
                    exc_info["parse_file_type"] = parse_file_type
                    fire_event(PartialParsingError(exc_info=exc_info))

                    # Send event
                    if dbt.tracking.active_user is not None:
                        exc_info["full_reparse_reason"] = ReparseReason.exception
                        dbt.tracking.track_partial_parser(exc_info)

                    if os.environ.get("DBT_PP_TEST"):
                        raise exc

        if self.manifest._parsing_info is None:
            self.manifest._parsing_info = ParsingInfo()

        if skip_parsing:
            fire_event(PartialParsingSkipParsing())
        else:
            # Load Macros and tests
            # We need to parse the macros first, so they're resolvable when
            # the other files are loaded.  Also need to parse tests, specifically
            # generic tests
            start_load_macros = time.perf_counter()
            self.load_and_parse_macros(project_parser_files)

            # If we're partially parsing check that certain macros have not been changed
            if self.partially_parsing and self.skip_partial_parsing_because_of_macros():
                fire_event(
                    UnableToPartialParse(
                        reason="change detected to override macro. Starting full parse."
                    )
                )

                # Get new Manifest with original file records and move over the macros
                self.manifest = self.new_manifest  # contains newly read files
                project_parser_files = orig_project_parser_files
                self.partially_parsing = False
                self.load_and_parse_macros(project_parser_files)

            self._perf_info.load_macros_elapsed = time.perf_counter() - start_load_macros

            # Now that the macros are parsed, parse the rest of the files.
            # This is currently done on a per project basis.
            start_parse_projects = time.perf_counter()

            # Load the rest of the files except for schema yaml files
            parser_types: List[Type[Parser]] = [
                ModelParser,
                SnapshotParser,
                AnalysisParser,
                SingularTestParser,
                SeedParser,
                DocumentationParser,
                HookParser,
            ]
            for project in self.all_projects.values():
                if project.project_name not in project_parser_files:
                    continue
                self.parse_project(
                    project, project_parser_files[project.project_name], parser_types
                )

            # Now that we've loaded most of the nodes (except for schema tests, sources, metrics)
            # load up the Lookup objects to resolve them by name, so the SourceFiles store
            # the unique_id instead of the name. Sources are loaded from yaml files, so
            # aren't in place yet
            self.manifest.rebuild_ref_lookup()
            self.manifest.rebuild_doc_lookup()
            self.manifest.rebuild_disabled_lookup()

            # Load yaml files
            parser_types = [SchemaParser]
            for project in self.all_projects.values():
                if project.project_name not in project_parser_files:
                    continue
                self.parse_project(
                    project, project_parser_files[project.project_name], parser_types
                )

            self.cleanup_disabled()

            self._perf_info.parse_project_elapsed = time.perf_counter() - start_parse_projects

            # patch_sources converts the UnparsedSourceDefinitions in the
            # Manifest.sources to SourceDefinition via 'patch_source'
            # in SourcePatcher
            start_patch = time.perf_counter()
            patcher = SourcePatcher(self.root_project, self.manifest)
            patcher.construct_sources()
            self.manifest.sources = patcher.sources
            self._perf_info.patch_sources_elapsed = time.perf_counter() - start_patch

            # We need to rebuild disabled in order to include disabled sources
            self.manifest.rebuild_disabled_lookup()

            # copy the selectors from the root_project to the manifest
            self.manifest.selectors = self.root_project.manifest_selectors

            # load manifest.dependencies and associated publication artifacts to create the external nodes
            public_nodes_changed = self.build_public_nodes()
            if public_nodes_changed:
                self.manifest.rebuild_ref_lookup()

            # update the refs, sources, docs and metrics depends_on.nodes
            # These check the created_at time on the nodes to
            # determine whether they need processing.
            start_process = time.perf_counter()
            self.process_sources(self.root_project.project_name)
            self.process_refs(self.root_project.project_name)
            self.process_docs(self.root_project)
            self.process_metrics(self.root_project)
            self.process_semantic_models()
            self.check_valid_group_config()
            self.check_valid_access_property()

            # update tracking data
            self._perf_info.process_manifest_elapsed = time.perf_counter() - start_process
            self._perf_info.static_analysis_parsed_path_count = (
                self.manifest._parsing_info.static_analysis_parsed_path_count
            )
            self._perf_info.static_analysis_path_count = (
                self.manifest._parsing_info.static_analysis_path_count
            )

        # Load dependencies, load the publication artifacts and create the external nodes.
        # Reprocess refs if changes.
        public_nodes_changed = False
        if skip_parsing:
            # If we didn't skip parsing, this will have already run because it must run
            # before process_refs. If we did skip parsing, then it's possible that only
            # publications have changed and we need to run this to capture that.
            self.manifest.build_parent_and_child_maps()
            public_nodes_changed = self.build_public_nodes()
            if public_nodes_changed:
                self.manifest.rebuild_ref_lookup()
                self.process_refs(self.root_project.project_name)
                # parent and child maps will be rebuilt by write_manifest

        # Log the publication artifact for this project
        log_publication_artifact(self.root_project, self.manifest)

        if not skip_parsing:
            # write out the fully parsed manifest
            self.write_manifest_for_partial_parse()

        self.check_for_model_deprecations()

        return self.manifest

    def check_for_model_deprecations(self):
        for node in self.manifest.nodes.values():
            if isinstance(node, ModelNode):
                if (
                    node.deprecation_date
                    and node.deprecation_date < datetime.datetime.now().astimezone()
                ):
                    fire_event(
                        DeprecatedModel(
                            model_name=node.name,
                            model_version=version_to_str(node.version),
                            deprecation_date=node.deprecation_date.isoformat(),
                        )
                    )

                resolved_refs = self.manifest.resolve_refs(node, self.root_project.project_name)
                resolved_model_refs = [r for r in resolved_refs if isinstance(r, ModelNode)]
                for resolved_ref in resolved_model_refs:
                    if resolved_ref.deprecation_date:

                        if resolved_ref.deprecation_date < datetime.datetime.now().astimezone():
                            event_cls = DeprecatedReference
                        else:
                            event_cls = UpcomingReferenceDeprecation

                        fire_event(
                            event_cls(
                                model_name=node.name,
                                ref_model_package=resolved_ref.package_name,
                                ref_model_name=resolved_ref.name,
                                ref_model_version=version_to_str(resolved_ref.version),
                                ref_model_latest_version=str(resolved_ref.latest_version),
                                ref_model_deprecation_date=resolved_ref.deprecation_date.isoformat(),
                            )
                        )

    def load_and_parse_macros(self, project_parser_files):
        for project in self.all_projects.values():
            if project.project_name not in project_parser_files:
                continue
            parser_files = project_parser_files[project.project_name]
            if "MacroParser" in parser_files:
                parser = MacroParser(project, self.manifest)
                for file_id in parser_files["MacroParser"]:
                    block = FileBlock(self.manifest.files[file_id])
                    parser.parse_file(block)
                    # increment parsed path count for performance tracking
                    self._perf_info.parsed_path_count += 1
            # generic tests hisotrically lived in the macros directoy but can now be nested
            # in a /generic directory under /tests so we want to process them here as well
            if "GenericTestParser" in parser_files:
                parser = GenericTestParser(project, self.manifest)
                for file_id in parser_files["GenericTestParser"]:
                    block = FileBlock(self.manifest.files[file_id])
                    parser.parse_file(block)
                    # increment parsed path count for performance tracking
                    self._perf_info.parsed_path_count += 1

        self.build_macro_resolver()
        # Look at changed macros and update the macro.depends_on.macros
        self.macro_depends_on()

    # Parse the files in the 'parser_files' dictionary, for parsers listed in
    # 'parser_types'
    def parse_project(
        self,
        project: Project,
        parser_files,
        parser_types: List[Type[Parser]],
    ) -> None:

        project_loader_info = self._perf_info._project_index[project.project_name]
        start_timer = time.perf_counter()
        total_parsed_path_count = 0

        # Loop through parsers with loaded files.
        for parser_cls in parser_types:
            parser_name = parser_cls.__name__
            # No point in creating a parser if we don't have files for it
            if parser_name not in parser_files or not parser_files[parser_name]:
                continue

            # Initialize timing info
            project_parsed_path_count = 0
            parser_start_timer = time.perf_counter()

            # Parse the project files for this parser
            parser: Parser = parser_cls(project, self.manifest, self.root_project)
            for file_id in parser_files[parser_name]:
                block = FileBlock(self.manifest.files[file_id])
                if isinstance(parser, SchemaParser):
                    assert isinstance(block.file, SchemaSourceFile)
                    if self.partially_parsing:
                        dct = block.file.pp_dict
                    else:
                        dct = block.file.dict_from_yaml
                    # this is where the schema file gets parsed
                    parser.parse_file(block, dct=dct)
                    # Came out of here with UnpatchedSourceDefinition containing configs at the source level
                    # and not configs at the table level (as expected)
                else:
                    parser.parse_file(block)
                project_parsed_path_count += 1

            # Save timing info
            project_loader_info.parsers.append(
                ParserInfo(
                    parser=parser.resource_type,
                    parsed_path_count=project_parsed_path_count,
                    elapsed=time.perf_counter() - parser_start_timer,
                )
            )
            total_parsed_path_count += project_parsed_path_count

        # HookParser doesn't run from loaded files, just dbt_project.yml,
        # so do separately
        # This shouldn't need to be parsed again if we're starting from
        # a saved manifest, because that won't be allowed if dbt_project.yml
        # changed, but leave for now.
        if not self.partially_parsing and HookParser in parser_types:
            hook_parser = HookParser(project, self.manifest, self.root_project)
            path = hook_parser.get_path()
            file = load_source_file(path, ParseFileType.Hook, project.project_name, {})
            if file:
                file_block = FileBlock(file)
                hook_parser.parse_file(file_block)

        # Store the performance info
        elapsed = time.perf_counter() - start_timer
        project_loader_info.parsed_path_count = (
            project_loader_info.parsed_path_count + total_parsed_path_count
        )
        project_loader_info.elapsed += elapsed
        self._perf_info.parsed_path_count = (
            self._perf_info.parsed_path_count + total_parsed_path_count
        )

    # This should only be called after the macros have been loaded
    def build_macro_resolver(self):
        internal_package_names = get_adapter_package_names(self.root_project.credentials.type)
        self.macro_resolver = MacroResolver(
            self.manifest.macros, self.root_project.project_name, internal_package_names
        )

    # Loop through macros in the manifest and statically parse
    # the 'macro_sql' to find depends_on.macros
    def macro_depends_on(self):
        macro_ctx = generate_macro_context(self.root_project)
        macro_namespace = TestMacroNamespace(self.macro_resolver, {}, None, MacroStack(), [])
        adapter = get_adapter(self.root_project)
        db_wrapper = ParseProvider().DatabaseWrapper(adapter, macro_namespace)
        for macro in self.manifest.macros.values():
            if macro.created_at < self.started_at:
                continue
            possible_macro_calls = statically_extract_macro_calls(
                macro.macro_sql, macro_ctx, db_wrapper
            )
            for macro_name in possible_macro_calls:
                # adapter.dispatch calls can generate a call with the same name as the macro
                # it ought to be an adapter prefix (postgres_) or default_
                if macro_name == macro.name:
                    continue
                package_name = macro.package_name
                if "." in macro_name:
                    package_name, macro_name = macro_name.split(".")
                dep_macro_id = self.macro_resolver.get_macro_id(package_name, macro_name)
                if dep_macro_id:
                    macro.depends_on.add_macro(dep_macro_id)  # will check for dupes

    def write_manifest_for_partial_parse(self):
        path = os.path.join(self.root_project.project_target_path, PARTIAL_PARSE_FILE_NAME)
        try:
            # This shouldn't be necessary, but we have gotten bug reports (#3757) of the
            # saved manifest not matching the code version.
            if self.manifest.metadata.dbt_version != __version__:
                fire_event(
                    UnableToPartialParse(reason="saved manifest contained the wrong version")
                )
                self.manifest.metadata.dbt_version = __version__
            manifest_msgpack = self.manifest.to_msgpack(extended_mashumaro_encoder)
            make_directory(os.path.dirname(path))
            with open(path, "wb") as fp:
                fp.write(manifest_msgpack)
        except Exception:
            raise

    def build_public_nodes(self) -> bool:
        """This method loads the dependencies from dependencies.yml, reads in the
        the publication artifacts and adds the PublicModels to the manifest
        "public_nodes" dictionary. It returns a boolean that indicates that
        public nodes have been rebuilt."""
        public_nodes_changed = False

        # Load the dependencies from the dependencies.yml file
        # TODO: dependencies might be better in the RuntimeConfig and
        # loaded somewhere earlier, but leaving this here for later refactoring.
        # Loading it elsewhere would make it harder to detect that there were
        # no dependencies previously and still are none, though that could be
        # inferred from the manifest publication configs.
        dependencies_filepath = resolve_path_from_base(
            DEPENDENCIES_FILE_NAME, self.root_project.project_root
        )
        saved_manifest_dependencies = self.manifest.project_dependencies
        if path_exists(dependencies_filepath):
            contents = load_file_contents(dependencies_filepath)
            dependencies_dict = load_yaml_text(contents)
            dependencies = ProjectDependencies.from_dict(dependencies_dict)
            self.manifest.project_dependencies = dependencies
        else:
            self.manifest.project_dependencies = None

        # Return False if there weren't any dependencies before and aren't any now.
        if saved_manifest_dependencies is None and self.manifest.project_dependencies is None:
            return False

        # collect the names of the projects for later use
        project_dependency_names = []
        if self.manifest.project_dependencies:
            for project in self.manifest.project_dependencies.projects:
                project_dependency_names.append(project.name)

        # Save previous publications, for later removal of references
        saved_manifest_publications: MutableMapping[str, PublicationConfig] = deepcopy(
            self.manifest.publications
        )
        if self.manifest.publications:
            for project_name, publication in self.manifest.publications.items():
                if project_name not in project_dependency_names:
                    remove_dependent_project_references(self.manifest, publication)
                    saved_manifest_publications.pop(project_name)
                    fire_event(
                        PublicationArtifactChanged(
                            action="removed",
                            project_name=project_name,
                            generated_at=datetime_to_json_string(
                                publication.metadata.generated_at
                            ),
                        )
                    )
                    public_nodes_changed = True

            # clean up previous publications that are no longer specified
            self.manifest.publications = {}
            # Empty public_nodes since we're re-generating them all
            self.manifest.public_nodes = {}

        if self.manifest.project_dependencies:
            self.load_new_public_nodes()

        # Now that we've loaded the current publications and public_nodes, look for
        # changed publications so we can reset the public_nodes references
        for project_name, publication in self.manifest.publications.items():
            if (
                project_name in saved_manifest_publications
                and publication.metadata.generated_at
                != saved_manifest_publications[project_name].metadata.generated_at
            ):
                remove_dependent_project_references(
                    self.manifest, saved_manifest_publications[project_name]
                )
                fire_event(
                    PublicationArtifactChanged(
                        action="updated",
                        project_name=project_name,
                        generated_at=datetime_to_json_string(publication.metadata.generated_at),
                    )
                )
                public_nodes_changed = True
            elif project_name not in saved_manifest_publications:
                fire_event(
                    PublicationArtifactChanged(
                        action="added",
                        project_name=project_name,
                        generated_at=datetime_to_json_string(publication.metadata.generated_at),
                    )
                )
                public_nodes_changed = True

        return public_nodes_changed

    def load_new_public_nodes(self):
        for project in self.manifest.project_dependencies.projects:
            try:
                publication = self.publications[project.name]
            except KeyError:
                raise PublicationConfigNotFound(project=project.name)

            publication_config = PublicationConfig.from_publication(publication)
            self.manifest.publications[project.name] = publication_config

            # Add to dictionary of public_nodes and save id in PublicationConfig
            for public_node in publication.public_models.values():
                self.manifest.public_nodes[public_node.unique_id] = public_node

    def is_partial_parsable(self, manifest: Manifest) -> Tuple[bool, Optional[str]]:
        """Compare the global hashes of the read-in parse results' values to
        the known ones, and return if it is ok to re-use the results.
        """
        valid = True
        reparse_reason = None

        if manifest.metadata.dbt_version != __version__:
            # #3757 log both versions because of reports of invalid cases of mismatch.
            fire_event(UnableToPartialParse(reason="of a version mismatch"))
            # If the version is wrong, the other checks might not work
            return False, ReparseReason.version_mismatch
        if self.manifest.state_check.vars_hash != manifest.state_check.vars_hash:
            fire_event(
                UnableToPartialParse(
                    reason="config vars, config profile, or config target have changed"
                )
            )
            fire_event(
                Note(
                    msg=f"previous checksum: {self.manifest.state_check.vars_hash.checksum}, current checksum: {manifest.state_check.vars_hash.checksum}"
                ),
                level=EventLevel.DEBUG,
            )
            valid = False
            reparse_reason = ReparseReason.vars_changed
        if self.manifest.state_check.profile_hash != manifest.state_check.profile_hash:
            # Note: This should be made more granular. We shouldn't need to invalidate
            # partial parsing if a non-used profile section has changed.
            fire_event(UnableToPartialParse(reason="profile has changed"))
            valid = False
            reparse_reason = ReparseReason.profile_changed
        if (
            self.manifest.state_check.project_env_vars_hash
            != manifest.state_check.project_env_vars_hash
        ):
            fire_event(
                UnableToPartialParse(reason="env vars used in dbt_project.yml have changed")
            )
            valid = False
            reparse_reason = ReparseReason.proj_env_vars_changed
        if (
            self.manifest.state_check.profile_env_vars_hash
            != manifest.state_check.profile_env_vars_hash
        ):
            fire_event(UnableToPartialParse(reason="env vars used in profiles.yml have changed"))
            valid = False
            reparse_reason = ReparseReason.prof_env_vars_changed

        missing_keys = {
            k
            for k in self.manifest.state_check.project_hashes
            if k not in manifest.state_check.project_hashes
        }
        if missing_keys:
            fire_event(UnableToPartialParse(reason="a project dependency has been added"))
            valid = False
            reparse_reason = ReparseReason.deps_changed

        for key, new_value in self.manifest.state_check.project_hashes.items():
            if key in manifest.state_check.project_hashes:
                old_value = manifest.state_check.project_hashes[key]
                if new_value != old_value:
                    fire_event(UnableToPartialParse(reason="a project config has changed"))
                    valid = False
                    reparse_reason = ReparseReason.project_config_changed
        return valid, reparse_reason

    def skip_partial_parsing_because_of_macros(self):
        if not self.partial_parser:
            return False
        if self.partial_parser.deleted_special_override_macro:
            return True
        # Check for custom versions of these special macros
        for macro_name in special_override_macros:
            macro = self.macro_resolver.get_macro(None, macro_name)
            if macro and macro.package_name != "dbt":
                if (
                    macro.file_id in self.partial_parser.file_diff["changed"]
                    or macro.file_id in self.partial_parser.file_diff["added"]
                ):
                    # The file with the macro in it has changed
                    return True
        return False

    def read_manifest_for_partial_parse(self) -> Optional[Manifest]:
        if not get_flags().PARTIAL_PARSE:
            fire_event(PartialParsingNotEnabled())
            return None
        path = os.path.join(self.root_project.project_target_path, PARTIAL_PARSE_FILE_NAME)

        reparse_reason = None

        if os.path.exists(path):
            try:
                with open(path, "rb") as fp:
                    manifest_mp = fp.read()
                manifest: Manifest = Manifest.from_msgpack(manifest_mp, decoder=extended_mashumuro_decoder)  # type: ignore
                # keep this check inside the try/except in case something about
                # the file has changed in weird ways, perhaps due to being a
                # different version of dbt
                is_partial_parsable, reparse_reason = self.is_partial_parsable(manifest)
                if is_partial_parsable:
                    # We don't want to have stale generated_at dates
                    manifest.metadata.generated_at = datetime.datetime.utcnow()
                    # or invocation_ids
                    manifest.metadata.invocation_id = get_invocation_id()
                    return manifest
            except Exception as exc:
                fire_event(
                    ParsedFileLoadFailed(path=path, exc=str(exc), exc_info=traceback.format_exc())
                )
                reparse_reason = ReparseReason.load_file_failure
        else:
            fire_event(
                UnableToPartialParse(reason="saved manifest not found. Starting full parse.")
            )
            reparse_reason = ReparseReason.file_not_found

        # this event is only fired if a full reparse is needed
        if dbt.tracking.active_user is not None:  # no active_user if doing load_macros
            dbt.tracking.track_partial_parser({"full_reparse_reason": reparse_reason})

        return None

    def build_perf_info(self):
        flags = get_flags()
        mli = ManifestLoaderInfo(
            is_partial_parse_enabled=flags.PARTIAL_PARSE,
            is_static_analysis_enabled=flags.STATIC_PARSER,
        )
        for project in self.all_projects.values():
            project_info = ProjectLoaderInfo(
                project_name=project.project_name,
                elapsed=0,
            )
            mli.projects.append(project_info)
            mli._project_index[project.project_name] = project_info
        return mli

    # TODO: handle --vars in the same way we handle env_var
    # https://github.com/dbt-labs/dbt-core/issues/6323
    def build_manifest_state_check(self):
        config = self.root_project
        all_projects = self.all_projects
        # if any of these change, we need to reject the parser

        # Create a FileHash of vars string, profile name and target name
        # This does not capture vars in dbt_project, just the command line
        # arg vars, but since any changes to that file will cause state_check
        # to not pass, it doesn't matter.  If we move to more granular checking
        # of env_vars, that would need to change.
        # We are using the parsed cli_vars instead of config.args.vars, in order
        # to sort them and avoid reparsing because of ordering issues.
        stringified_cli_vars = pprint.pformat(config.cli_vars)
        vars_hash = FileHash.from_contents(
            "\x00".join(
                [
                    stringified_cli_vars,
                    getattr(config.args, "profile", "") or "",
                    getattr(config.args, "target", "") or "",
                    __version__,
                ]
            )
        )
        fire_event(
            StateCheckVarsHash(
                checksum=vars_hash.checksum,
                vars=stringified_cli_vars,
                profile=config.args.profile,
                target=config.args.target,
                version=__version__,
            )
        )

        # Create a FileHash of the env_vars in the project
        key_list = list(config.project_env_vars.keys())
        key_list.sort()
        env_var_str = ""
        for key in key_list:
            env_var_str += f"{key}:{config.project_env_vars[key]}|"
        project_env_vars_hash = FileHash.from_contents(env_var_str)

        # Create a FileHash of the env_vars in the project
        key_list = list(config.profile_env_vars.keys())
        key_list.sort()
        env_var_str = ""
        for key in key_list:
            env_var_str += f"{key}:{config.profile_env_vars[key]}|"
        profile_env_vars_hash = FileHash.from_contents(env_var_str)

        # Create a FileHash of the profile file
        profile_path = os.path.join(get_flags().PROFILES_DIR, "profiles.yml")
        with open(profile_path) as fp:
            profile_hash = FileHash.from_contents(fp.read())

        # Create a FileHashes for dbt_project for all dependencies
        project_hashes = {}
        for name, project in all_projects.items():
            path = os.path.join(project.project_root, "dbt_project.yml")
            with open(path) as fp:
                project_hashes[name] = FileHash.from_contents(fp.read())

        # Create the ManifestStateCheck object
        state_check = ManifestStateCheck(
            project_env_vars_hash=project_env_vars_hash,
            profile_env_vars_hash=profile_env_vars_hash,
            vars_hash=vars_hash,
            profile_hash=profile_hash,
            project_hashes=project_hashes,
        )
        return state_check

    def save_macros_to_adapter(self, adapter):
        macro_manifest = MacroManifest(self.manifest.macros)
        adapter._macro_manifest_lazy = macro_manifest
        # This executes the callable macro_hook and sets the
        # query headers
        self.macro_hook(macro_manifest)

    # This creates a MacroManifest which contains the macros in
    # the adapter. Only called by the load_macros call from the
    # adapter.
    def create_macro_manifest(self):
        for project in self.all_projects.values():
            # what is the manifest passed in actually used for?
            macro_parser = MacroParser(project, self.manifest)
            for path in macro_parser.get_paths():
                source_file = load_source_file(path, ParseFileType.Macro, project.project_name, {})
                block = FileBlock(source_file)
                # This does not add the file to the manifest.files,
                # but that shouldn't be necessary here.
                macro_parser.parse_file(block)
        macro_manifest = MacroManifest(self.manifest.macros)
        return macro_manifest

    # This is called by the adapter code only, to create the
    # MacroManifest that's stored in the adapter.
    # 'get_full_manifest' uses a persistent ManifestLoader while this
    # creates a temporary ManifestLoader and throws it away.
    # Not sure when this would actually get used except in tests.
    # The ManifestLoader loads macros with other files, then copies
    # into the adapter MacroManifest.
    @classmethod
    def load_macros(
        cls,
        root_config: RuntimeConfig,
        macro_hook: Callable[[Manifest], Any],
        base_macros_only=False,
    ) -> Manifest:
        with PARSING_STATE:
            # base_only/base_macros_only: for testing only,
            # allows loading macros without running 'dbt deps' first
            projects = root_config.load_dependencies(base_only=base_macros_only)

            # This creates a loader object, including result,
            # and then throws it away, returning only the
            # manifest
            loader = cls(root_config, projects, macro_hook)
            macro_manifest = loader.create_macro_manifest()

        return macro_manifest

    # Create tracking event for saving performance info
    def track_project_load(self):
        invocation_id = get_invocation_id()
        dbt.tracking.track_project_load(
            {
                "invocation_id": invocation_id,
                "project_id": self.root_project.hashed_name(),
                "path_count": self._perf_info.path_count,
                "parsed_path_count": self._perf_info.parsed_path_count,
                "read_files_elapsed": self._perf_info.read_files_elapsed,
                "load_macros_elapsed": self._perf_info.load_macros_elapsed,
                "parse_project_elapsed": self._perf_info.parse_project_elapsed,
                "patch_sources_elapsed": self._perf_info.patch_sources_elapsed,
                "process_manifest_elapsed": (self._perf_info.process_manifest_elapsed),
                "load_all_elapsed": self._perf_info.load_all_elapsed,
                "is_partial_parse_enabled": (self._perf_info.is_partial_parse_enabled),
                "is_static_analysis_enabled": self._perf_info.is_static_analysis_enabled,
                "static_analysis_path_count": self._perf_info.static_analysis_path_count,
                "static_analysis_parsed_path_count": self._perf_info.static_analysis_parsed_path_count,  # noqa: E501
            }
        )

    # Takes references in 'refs' array of nodes and exposures, finds the target
    # node, and updates 'depends_on.nodes' with the unique id
    def process_refs(self, current_project: str):
        for node in self.manifest.nodes.values():
            if node.created_at < self.started_at:
                continue
            _process_refs_for_node(self.manifest, current_project, node)
        for exposure in self.manifest.exposures.values():
            if exposure.created_at < self.started_at:
                continue
            _process_refs_for_exposure(self.manifest, current_project, exposure)
        for metric in self.manifest.metrics.values():
            if metric.created_at < self.started_at:
                continue
            _process_refs_for_metric(self.manifest, current_project, metric)

    # Takes references in 'metrics' array of nodes and exposures, finds the target
    # node, and updates 'depends_on.nodes' with the unique id
    def process_metrics(self, config: RuntimeConfig):
        current_project = config.project_name
        for node in self.manifest.nodes.values():
            if node.created_at < self.started_at:
                continue
            _process_metrics_for_node(self.manifest, current_project, node)
        for metric in self.manifest.metrics.values():
            # TODO: Can we do this if the metric is derived & depends on
            # some other metric for its definition? Maybe....
            if metric.created_at < self.started_at:
                continue
            _process_metrics_for_node(self.manifest, current_project, metric)
        for exposure in self.manifest.exposures.values():
            if exposure.created_at < self.started_at:
                continue
            _process_metrics_for_node(self.manifest, current_project, exposure)

    def process_semantic_models(self) -> None:
        for semantic_model in self.manifest.semantic_nodes.values():
            if semantic_model.model:
                statically_parsed = py_extract_from_source(f"{{{{ {semantic_model.model} }}}}")
                if statically_parsed["refs"]:

                    ref = statically_parsed["refs"][0]
                    if len(ref) == 2:
                        input_package_name, input_model_name = ref
                    else:
                        input_package_name, input_model_name = None, ref[0]

                    refd_node = self.manifest.ref_lookup.find(
                        input_model_name, input_package_name, None, self.manifest
                    )
                    if isinstance(refd_node, ModelNode):
                        semantic_model.node_relation = NodeRelation(
                            alias=refd_node.alias,
                            schema_name=refd_node.schema,
                            database=refd_node.database,
                        )

    # nodes: node and column descriptions
    # sources: source and table descriptions, column descriptions
    # macros: macro argument descriptions
    # exposures: exposure descriptions
    def process_docs(self, config: RuntimeConfig):
        for node in self.manifest.nodes.values():
            if node.created_at < self.started_at:
                continue
            ctx = generate_runtime_docs_context(
                config,
                node,
                self.manifest,
                config.project_name,
            )
            _process_docs_for_node(ctx, node)
        for source in self.manifest.sources.values():
            if source.created_at < self.started_at:
                continue
            ctx = generate_runtime_docs_context(
                config,
                source,
                self.manifest,
                config.project_name,
            )
            _process_docs_for_source(ctx, source)
        for macro in self.manifest.macros.values():
            if macro.created_at < self.started_at:
                continue
            ctx = generate_runtime_docs_context(
                config,
                macro,
                self.manifest,
                config.project_name,
            )
            _process_docs_for_macro(ctx, macro)
        for exposure in self.manifest.exposures.values():
            if exposure.created_at < self.started_at:
                continue
            ctx = generate_runtime_docs_context(
                config,
                exposure,
                self.manifest,
                config.project_name,
            )
            _process_docs_for_exposure(ctx, exposure)
        for metric in self.manifest.metrics.values():
            if metric.created_at < self.started_at:
                continue
            ctx = generate_runtime_docs_context(
                config,
                metric,
                self.manifest,
                config.project_name,
            )
            _process_docs_for_metrics(ctx, metric)

    # Loops through all nodes and exposures, for each element in
    # 'sources' array finds the source node and updates the
    # 'depends_on.nodes' array with the unique id
    def process_sources(self, current_project: str):
        for node in self.manifest.nodes.values():
            if node.resource_type == NodeType.Source:
                continue
            assert not isinstance(node, SourceDefinition)
            if node.created_at < self.started_at:
                continue
            _process_sources_for_node(self.manifest, current_project, node)
        for exposure in self.manifest.exposures.values():
            if exposure.created_at < self.started_at:
                continue
            _process_sources_for_exposure(self.manifest, current_project, exposure)

    def cleanup_disabled(self):
        # make sure the nodes are in the manifest.nodes or the disabled dict,
        # correctly now that the schema files are also parsed
        disabled_nodes = []
        for node in self.manifest.nodes.values():
            if not node.config.enabled:
                disabled_nodes.append(node.unique_id)
                self.manifest.add_disabled_nofile(node)
        for unique_id in disabled_nodes:
            self.manifest.nodes.pop(unique_id)

        disabled_copy = deepcopy(self.manifest.disabled)
        for disabled in disabled_copy.values():
            for node in disabled:
                if node.config.enabled:
                    for dis_index, dis_node in enumerate(disabled):
                        # Remove node from disabled and unique_id from disabled dict if necessary
                        del self.manifest.disabled[node.unique_id][dis_index]
                        if not self.manifest.disabled[node.unique_id]:
                            self.manifest.disabled.pop(node.unique_id)

                    self.manifest.add_node_nofile(node)

        self.manifest.rebuild_ref_lookup()

    def check_valid_group_config(self):
        manifest = self.manifest
        group_names = {group.name for group in manifest.groups.values()}

        for metric in manifest.metrics.values():
            self.check_valid_group_config_node(metric, group_names)

        for node in manifest.nodes.values():
            self.check_valid_group_config_node(node, group_names)

    def check_valid_group_config_node(
        self, groupable_node: Union[Metric, ManifestNode], valid_group_names: Set[str]
    ):
        groupable_node_group = groupable_node.group
        if groupable_node_group and groupable_node_group not in valid_group_names:
            raise dbt.exceptions.ParsingError(
                f"Invalid group '{groupable_node_group}', expected one of {sorted(list(valid_group_names))}",
                node=groupable_node,
            )

    def check_valid_access_property(self):
        for node in self.manifest.nodes.values():
            if (
                isinstance(node, ModelNode)
                and node.access == AccessType.Public
                and node.get_materialization() == "ephemeral"
            ):
                raise InvalidAccessTypeError(
                    unique_id=node.unique_id,
                    field_value=node.access,
                    materialization=node.get_materialization(),
                )

    def write_perf_info(self, target_path: str):
        path = os.path.join(target_path, PERF_INFO_FILE_NAME)
        write_file(path, json.dumps(self._perf_info, cls=dbt.utils.JSONEncoder, indent=4))
        fire_event(ParsePerfInfoPath(path=path))


def invalid_target_fail_unless_test(
    node,
    target_name: str,
    target_kind: str,
    target_package: Optional[str] = None,
    target_version: Optional[NodeVersion] = None,
    disabled: Optional[bool] = None,
    should_warn_if_disabled: bool = True,
):
    if node.resource_type == NodeType.Test:
        if disabled:
            event = InvalidDisabledTargetInTestNode(
                resource_type_title=node.resource_type.title(),
                unique_id=node.unique_id,
                original_file_path=node.original_file_path,
                target_kind=target_kind,
                target_name=target_name,
                target_package=target_package if target_package else "",
            )

            fire_event(event, EventLevel.WARN if should_warn_if_disabled else None)
        else:
            warn_or_error(
                NodeNotFoundOrDisabled(
                    original_file_path=node.original_file_path,
                    unique_id=node.unique_id,
                    resource_type_title=node.resource_type.title(),
                    target_name=target_name,
                    target_kind=target_kind,
                    target_package=target_package if target_package else "",
                    disabled=str(disabled),
                )
            )
    else:
        raise TargetNotFoundError(
            node=node,
            target_name=target_name,
            target_kind=target_kind,
            target_package=target_package,
            target_version=target_version,
            disabled=disabled,
        )


def _check_resource_uniqueness(
    manifest: Manifest,
    config: RuntimeConfig,
) -> None:
    alias_resources: Dict[str, ManifestNode] = {}
    name_resources: Dict[str, Dict] = {}

    for resource, node in manifest.nodes.items():
        if not node.is_relational:
            continue

        if node.package_name not in name_resources:
            name_resources[node.package_name] = {"ver": {}, "unver": {}}
        if node.is_versioned:
            name_resources[node.package_name]["ver"][node.name] = node
        else:
            name_resources[node.package_name]["unver"][node.name] = node

        # the full node name is really defined by the adapter's relation
        relation_cls = get_relation_class_by_name(config.credentials.type)
        relation = relation_cls.create_from(config=config, node=node)
        full_node_name = str(relation)

        existing_alias = alias_resources.get(full_node_name)
        if existing_alias is not None:
            raise AmbiguousAliasError(
                node_1=existing_alias, node_2=node, duped_name=full_node_name
            )

        alias_resources[full_node_name] = node

    for ver_unver_dict in name_resources.values():
        versioned_names = ver_unver_dict["ver"].keys()
        unversioned_names = ver_unver_dict["unver"].keys()
        intersection_versioned = set(versioned_names).intersection(set(unversioned_names))
        if intersection_versioned:
            for name in intersection_versioned:
                versioned_node = ver_unver_dict["ver"][name]
                unversioned_node = ver_unver_dict["unver"][name]
                raise dbt.exceptions.DuplicateVersionedUnversionedError(
                    versioned_node, unversioned_node
                )


def _warn_for_unused_resource_config_paths(manifest: Manifest, config: RuntimeConfig) -> None:
    resource_fqns: Mapping[str, PathSet] = manifest.get_resource_fqns()
    disabled_fqns: PathSet = frozenset(
        tuple(n.fqn) for n in list(chain.from_iterable(manifest.disabled.values()))
    )
    config.warn_for_unused_resource_config_paths(resource_fqns, disabled_fqns)


def _check_manifest(manifest: Manifest, config: RuntimeConfig) -> None:
    _check_resource_uniqueness(manifest, config)
    _warn_for_unused_resource_config_paths(manifest, config)


DocsContextCallback = Callable[[ResultNode], Dict[str, Any]]


# node and column descriptions
def _process_docs_for_node(
    context: Dict[str, Any],
    node: ManifestNode,
):
    node.description = get_rendered(node.description, context)
    for column_name, column in node.columns.items():
        column.description = get_rendered(column.description, context)


# source and table descriptions, column descriptions
def _process_docs_for_source(
    context: Dict[str, Any],
    source: SourceDefinition,
):
    table_description = source.description
    source_description = source.source_description
    table_description = get_rendered(table_description, context)
    source_description = get_rendered(source_description, context)
    source.description = table_description
    source.source_description = source_description

    for column in source.columns.values():
        column_desc = column.description
        column_desc = get_rendered(column_desc, context)
        column.description = column_desc


# macro argument descriptions
def _process_docs_for_macro(context: Dict[str, Any], macro: Macro) -> None:
    macro.description = get_rendered(macro.description, context)
    for arg in macro.arguments:
        arg.description = get_rendered(arg.description, context)


# exposure descriptions
def _process_docs_for_exposure(context: Dict[str, Any], exposure: Exposure) -> None:
    exposure.description = get_rendered(exposure.description, context)


def _process_docs_for_metrics(context: Dict[str, Any], metric: Metric) -> None:
    metric.description = get_rendered(metric.description, context)


def _process_refs_for_exposure(manifest: Manifest, current_project: str, exposure: Exposure):
    """Given a manifest and exposure in that manifest, process its refs"""
    for ref in exposure.refs:
        target_model: Optional[Union[Disabled, ManifestNode]] = None
        target_model_name: str = ref.name
        target_model_package: Optional[str] = ref.package
        target_model_version: Optional[NodeVersion] = ref.version

        if len(ref.positional_args) < 1 or len(ref.positional_args) > 2:
            raise dbt.exceptions.DbtInternalError(
                f"Refs should always be 1 or 2 arguments - got {len(ref.positional_args)}"
            )

        target_model = manifest.resolve_ref(
            exposure,
            target_model_name,
            target_model_package,
            target_model_version,
            current_project,
            exposure.package_name,
        )

        if target_model is None or isinstance(target_model, Disabled):
            # This may raise. Even if it doesn't, we don't want to add
            # this exposure to the graph b/c there is no destination exposure
            exposure.config.enabled = False
            invalid_target_fail_unless_test(
                node=exposure,
                target_name=target_model_name,
                target_kind="node",
                target_package=target_model_package,
                target_version=target_model_version,
                disabled=(isinstance(target_model, Disabled)),
                should_warn_if_disabled=False,
            )

            continue
        elif isinstance(target_model, ModelNode) and target_model.access == AccessType.Private:
            # Exposures do not have a group and so can never reference private models
            raise dbt.exceptions.DbtReferenceError(
                unique_id=exposure.unique_id,
                ref_unique_id=target_model.unique_id,
                group=dbt.utils.cast_to_str(target_model.group),
            )

        target_model_id = target_model.unique_id

        exposure.depends_on.add_node(target_model_id)


def _process_refs_for_metric(manifest: Manifest, current_project: str, metric: Metric):
    """Given a manifest and a metric in that manifest, process its refs"""
    for ref in metric.refs:
        target_model: Optional[Union[Disabled, ManifestNode]] = None
        target_model_name: str = ref.name
        target_model_package: Optional[str] = ref.package
        target_model_version: Optional[NodeVersion] = ref.version

        if len(ref.positional_args) < 1 or len(ref.positional_args) > 2:
            raise dbt.exceptions.DbtInternalError(
                f"Refs should always be 1 or 2 arguments - got {len(ref.positional_args)}"
            )

        target_model = manifest.resolve_ref(
            metric,
            target_model_name,
            target_model_package,
            target_model_version,
            current_project,
            metric.package_name,
        )

        if target_model is None or isinstance(target_model, Disabled):
            # This may raise. Even if it doesn't, we don't want to add
            # this metric to the graph b/c there is no destination metric
            metric.config.enabled = False
            invalid_target_fail_unless_test(
                node=metric,
                target_name=target_model_name,
                target_kind="node",
                target_package=target_model_package,
                target_version=target_model_version,
                disabled=(isinstance(target_model, Disabled)),
                should_warn_if_disabled=False,
            )
            continue
        elif isinstance(target_model, ModelNode) and target_model.access == AccessType.Private:
            if not metric.group or metric.group != target_model.group:
                raise dbt.exceptions.DbtReferenceError(
                    unique_id=metric.unique_id,
                    ref_unique_id=target_model.unique_id,
                    group=dbt.utils.cast_to_str(target_model.group),
                )

        target_model_id = target_model.unique_id

        metric.depends_on.add_node(target_model_id)


def _process_metrics_for_node(
    manifest: Manifest,
    current_project: str,
    node: Union[ManifestNode, Metric, Exposure],
):
    """Given a manifest and a node in that manifest, process its metrics"""

    if isinstance(node, SeedNode):
        return

    for metric in node.metrics:
        target_metric: Optional[Union[Disabled, Metric]] = None
        target_metric_name: str
        target_metric_package: Optional[str] = None

        if len(metric) == 1:
            target_metric_name = metric[0]
        elif len(metric) == 2:
            target_metric_package, target_metric_name = metric
        else:
            raise dbt.exceptions.DbtInternalError(
                f"Metric references should always be 1 or 2 arguments - got {len(metric)}"
            )

        target_metric = manifest.resolve_metric(
            target_metric_name,
            target_metric_package,
            current_project,
            node.package_name,
        )

        if target_metric is None or isinstance(target_metric, Disabled):
            # This may raise. Even if it doesn't, we don't want to add
            # this node to the graph b/c there is no destination node
            node.config.enabled = False
            invalid_target_fail_unless_test(
                node=node,
                target_name=target_metric_name,
                target_kind="metric",
                target_package=target_metric_package,
                disabled=(isinstance(target_metric, Disabled)),
            )
            continue

        target_metric_id = target_metric.unique_id

        node.depends_on.add_node(target_metric_id)


def _process_refs_for_node(manifest: Manifest, current_project: str, node: ManifestNode):
    """Given a manifest and a node in that manifest, process its refs"""

    if isinstance(node, SeedNode):
        return

    for ref in node.refs:
        target_model: Optional[Union[Disabled, ManifestNode]] = None
        target_model_name: str = ref.name
        target_model_package: Optional[str] = ref.package
        target_model_version: Optional[NodeVersion] = ref.version

        if len(ref.positional_args) < 1 or len(ref.positional_args) > 2:
            raise dbt.exceptions.DbtInternalError(
                f"Refs should always be 1 or 2 arguments - got {len(ref.positional_args)}"
            )

        target_model = manifest.resolve_ref(
            node,
            target_model_name,
            target_model_package,
            target_model_version,
            current_project,
            node.package_name,
        )

        if target_model is None or isinstance(target_model, Disabled):
            # This may raise. Even if it doesn't, we don't want to add
            # this node to the graph b/c there is no destination node
            node.config.enabled = False
            invalid_target_fail_unless_test(
                node=node,
                target_name=target_model_name,
                target_kind="node",
                target_package=target_model_package,
                target_version=target_model_version,
                disabled=(isinstance(target_model, Disabled)),
                should_warn_if_disabled=False,
            )
            continue

        # Handle references to models that are private
        elif isinstance(target_model, ModelNode) and target_model.access == AccessType.Private:
            if not node.group or node.group != target_model.group:
                raise dbt.exceptions.DbtReferenceError(
                    unique_id=node.unique_id,
                    ref_unique_id=target_model.unique_id,
                    group=dbt.utils.cast_to_str(target_model.group),
                )

        target_model_id = target_model.unique_id

        if target_model.is_public_node:
            node.depends_on.add_public_node(target_model_id)
        else:
            node.depends_on.add_node(target_model_id)


def remove_dependent_project_references(manifest: Manifest, publication: PublicationConfig):
    for unique_id in publication.public_node_ids:
        for child_id in manifest.child_map[unique_id]:
            node = manifest.expect(child_id)
            if hasattr(node.depends_on, "public_nodes"):
                node.depends_on.public_nodes.remove(unique_id)  # type: ignore
                # set created_at so process_refs happens
                node.created_at = time.time()


def _process_sources_for_exposure(manifest: Manifest, current_project: str, exposure: Exposure):
    target_source: Optional[Union[Disabled, SourceDefinition]] = None
    for source_name, table_name in exposure.sources:
        target_source = manifest.resolve_source(
            source_name,
            table_name,
            current_project,
            exposure.package_name,
        )
        if target_source is None or isinstance(target_source, Disabled):
            exposure.config.enabled = False
            invalid_target_fail_unless_test(
                node=exposure,
                target_name=f"{source_name}.{table_name}",
                target_kind="source",
                disabled=(isinstance(target_source, Disabled)),
            )
            continue
        target_source_id = target_source.unique_id
        exposure.depends_on.add_node(target_source_id)


def _process_sources_for_metric(manifest: Manifest, current_project: str, metric: Metric):
    target_source: Optional[Union[Disabled, SourceDefinition]] = None
    for source_name, table_name in metric.sources:
        target_source = manifest.resolve_source(
            source_name,
            table_name,
            current_project,
            metric.package_name,
        )
        if target_source is None or isinstance(target_source, Disabled):
            metric.config.enabled = False
            invalid_target_fail_unless_test(
                node=metric,
                target_name=f"{source_name}.{table_name}",
                target_kind="source",
                disabled=(isinstance(target_source, Disabled)),
            )
            continue
        target_source_id = target_source.unique_id
        metric.depends_on.add_node(target_source_id)


def _process_sources_for_node(manifest: Manifest, current_project: str, node: ManifestNode):

    if isinstance(node, SeedNode):
        return

    target_source: Optional[Union[Disabled, SourceDefinition]] = None
    for source_name, table_name in node.sources:
        target_source = manifest.resolve_source(
            source_name,
            table_name,
            current_project,
            node.package_name,
        )

        if target_source is None or isinstance(target_source, Disabled):
            # this folows the same pattern as refs
            node.config.enabled = False
            invalid_target_fail_unless_test(
                node=node,
                target_name=f"{source_name}.{table_name}",
                target_kind="source",
                disabled=(isinstance(target_source, Disabled)),
            )
            continue
        target_source_id = target_source.unique_id
        node.depends_on.add_node(target_source_id)


# This is called in task.rpc.sql_commands when a "dynamic" node is
# created in the manifest, in 'add_refs'
def process_macro(config: RuntimeConfig, manifest: Manifest, macro: Macro) -> None:
    ctx = generate_runtime_docs_context(
        config,
        macro,
        manifest,
        config.project_name,
    )
    _process_docs_for_macro(ctx, macro)


# This is called in task.rpc.sql_commands when a "dynamic" node is
# created in the manifest, in 'add_refs'
def process_node(config: RuntimeConfig, manifest: Manifest, node: ManifestNode):

    _process_sources_for_node(manifest, config.project_name, node)
    _process_refs_for_node(manifest, config.project_name, node)
    ctx = generate_runtime_docs_context(config, node, manifest, config.project_name)
    _process_docs_for_node(ctx, node)


def log_publication_artifact(root_project: RuntimeConfig, manifest: Manifest):
    # The manifest.json is written out in a task, so we're not writing it here

    # build publication metadata
    metadata = PublicationMetadata(
        adapter_type=root_project.credentials.type,
        quoting=root_project.quoting,
    )

    # get a set of public model ids first so it can be used in constructing dependencies
    public_node_ids = {
        node.unique_id
        for node in manifest.nodes.values()
        if isinstance(node, ModelNode) and node.access == AccessType.Public
    }

    # Get the Graph object from the Linker
    from dbt.compilation import Linker

    linker = Linker()
    graph = linker.get_graph(manifest)

    public_models = {}
    for unique_id in public_node_ids:
        model = manifest.nodes[unique_id]
        assert isinstance(model, ModelNode)
        # public_node_dependencies is the intersection of all parent nodes plus public nodes
        parents: Set[UniqueId] = graph.select_parents({UniqueId(unique_id)})
        public_node_dependencies: Set[UniqueId] = parents.intersection(public_node_ids)

        public_model = PublicModel(
            name=model.name,
            package_name=model.package_name,
            unique_id=model.unique_id,
            relation_name=dbt.utils.cast_to_str(model.relation_name),
            database=model.database,
            schema=model.schema,
            identifier=model.alias,
            version=model.version,
            latest_version=model.latest_version,
            public_node_dependencies=list(public_node_dependencies),
            generated_at=metadata.generated_at,
            deprecation_date=model.deprecation_date,
        )
        public_models[unique_id] = public_model

    dependencies = []
    # Get dependencies from dependencies.yml
    if manifest.project_dependencies:
        for dep_project in manifest.project_dependencies.projects:
            dependencies.append(dep_project.name)
    # Get dependencies from publication dependencies
    for pub_project in manifest.publications.values():
        for project_name in pub_project.dependencies:
            if project_name == root_project.project_name:
                raise ProjectDependencyCycleError(
                    pub_project_name=pub_project.project_name, project_name=project_name
                )
            if project_name not in dependencies:
                dependencies.append(project_name)

    publication = PublicationArtifact(
        metadata=metadata,
        project_name=root_project.project_name,
        public_models=public_models,
        dependencies=dependencies,
    )

    fire_event(PublicationArtifactAvailable(pub_artifact=publication.to_dict()))


def write_semantic_manifest(manifest: Manifest, target_path: str) -> None:
    path = os.path.join(target_path, SEMANTIC_MANIFEST_FILE_NAME)
    write_file(path, manifest.pydantic_semantic_manifest.json())


def write_manifest(manifest: Manifest, target_path: str):
    path = os.path.join(target_path, MANIFEST_FILE_NAME)
    manifest.write(path)

    write_semantic_manifest(manifest=manifest, target_path=target_path)
