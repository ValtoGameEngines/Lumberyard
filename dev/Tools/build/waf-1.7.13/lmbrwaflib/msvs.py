#! /usr/bin/env python
# encoding: utf-8

# Modifications copyright Amazon.com, Inc. or its affiliates.

"""
Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions
are met:

1. Redistributions of source code must retain the above copyright
   notice, this list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright
   notice, this list of conditions and the following disclaimer in the
   documentation and/or other materials provided with the distribution.

3. The name of the author may not be used to endorse or promote products
   derived from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE AUTHOR "AS IS" AND ANY EXPRESS OR
IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY DIRECT,
INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
(INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING
IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
POSSIBILITY OF SUCH DAMAGE.
"""

"""
To add this tool to your project:
def options(conf):
	opt.load('msvs')

It can be a good idea to add the sync_exec tool too.

To generate solution files:
$ waf configure msvs

To customize the outputs, provide subclasses in your wscript files:

from waflib.extras import msvs
class vsnode_target(msvs.vsnode_target):
	def get_build_command(self, props):
		# likely to be required
		return "waf.bat build"
	def collect_source(self):
		# likely to be required
		...
class msvs_bar(msvs.msvs_generator):
	def init(self):
		msvs.msvs_generator.init(self)
		self.vsnode_target = vsnode_target

The msvs class re-uses the same build() function for reading the targets (task generators),
you may therefore specify msvs settings on the context object:

def build(bld):
	bld.solution_name = 'foo.sln'
	bld.waf_command = 'waf.bat'
	bld.projects_dir = bld.srcnode.make_node('foo.depproj')
	bld.projects_dir.mkdir()

For visual studio 2008, the command is called 'msvs2008', and the classes
such as vsnode_target are wrapped by a decorator class 'wrap_2008' to
provide special functionality.

ASSUMPTIONS:
* a project can be either a directory or a target, vcxproj files are written only for targets that have source files
* each project is a vcxproj file, therefore the project uuid needs only to be a hash of the absolute path
"""

# System Imports
import datetime
import os
import re
import sys
import time
import uuid  # requires python 2.5

try:
    import winreg
    WINREG_SUPPORTED = True
except ImportError:
    WINREG_SUPPORTED = False
    pass

# waflib imports
from waflib.Build import BuildContext
from waflib import Utils, TaskGen, Logs, Task, Context, Node, Options, Errors
from waflib.Configure import conf, conf_event, ConfigurationContext, deprecated
from waflib.ConfigSet import ConfigSet

# lmbrwaflib imports
from lmbrwaflib import cry_utils
from lmbrwaflib import msvc_helper
from lmbrwaflib import settings_manager
from lmbrwaflib.cry_utils import append_to_unique_list, split_comma_delimited_string
from lmbrwaflib.lumberyard_modules import apply_project_settings_for_input


HEADERS_GLOB = '**/(*.h|*.hpp|*.H|*.inl|*.hxx)'

PROJECT_TEMPLATE = r'''<?xml version="1.0" encoding="UTF-8"?>
<Project DefaultTargets="Build" ToolsVersion="${project.msvs_version}.0" xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
	<ItemGroup Label="ProjectConfigurations">
		${for b in project.build_properties}
			<ProjectConfiguration Include="${b.configuration_name}|${b.platform.split()[0]}">
				<Configuration>${b.configuration_name}</Configuration>
				<Platform>${b.platform.split()[0]}</Platform>
			</ProjectConfiguration>
		${endfor}
	</ItemGroup>
	<PropertyGroup Label="Globals">
		<ProjectGuid>{${project.uuid}}</ProjectGuid>
		<Keyword>MakefileProj</Keyword>
		<ProjectName>${project.name}</ProjectName>
		${if hasattr(project, 'vs_globals')}
		    ${for vs_prop, vs_global in list(project.vs_globals.items())}
		        <${vs_prop}>${vs_global}</${vs_prop}>
			${endfor}
		${endif}
	</PropertyGroup>
	<Import Project="$(VCTargetsPath)\Microsoft.Cpp.Default.props" />
	${for b in project.build_properties}
		<PropertyGroup Condition="'$(Configuration)|$(Platform)'=='${b.configuration_name}|${b.platform.split()[0]}'" Label="Configuration">
			<ConfigurationType>Makefile</ConfigurationType>
			<OutDir>${b.outdir}</OutDir>
			<BinDir>${b.bindir}</BinDir>
			${if b.platform == 'ARM'}
				<Keyword>Android</Keyword>
				<PlatformToolset>Gcc_4_9</PlatformToolset>
				<ApplicationType>Android</ApplicationType>
				<AndroidAPILevel>android-21</AndroidAPILevel>
			${else}
				<PlatformToolset>${project.get_platform_toolset(b.platform)}</PlatformToolset>
			${endif}
		</PropertyGroup>
	${endfor}
	<Import Project="$(VCTargetsPath)\Microsoft.Cpp.props" />
	<ImportGroup Label="ExtensionSettings" />
	<ImportGroup Label="PropertySheets" Condition="${project.get_all_config_platform_conditional_trimmed()}">
		<Import Project="$(MSBuildProjectDirectory)\$(MSBuildProjectName).vcxproj.default.props" Condition="exists('$(MSBuildProjectDirectory)\$(MSBuildProjectName).vcxproj.default.props')"/>
		<Import Project="$(UserRootDir)\Microsoft.Cpp.$(Platform).user.props" Condition="exists('$(UserRootDir)\Microsoft.Cpp.$(Platform).user.props')" Label="LocalAppDataPlatform" />
	</ImportGroup>
	${for b in project.build_properties}
		<PropertyGroup Condition="'$(Configuration)|$(Platform)'=='${b.configuration_name}|${b.platform.split()[0]}'">
			<NMakeBuildCommandLine>${xml:project.get_build_command(b)}</NMakeBuildCommandLine>
			<NMakeReBuildCommandLine>${xml:project.get_rebuild_command(b)}</NMakeReBuildCommandLine>
			<NMakeCleanCommandLine>${xml:project.get_clean_command(b)}</NMakeCleanCommandLine>
			<NMakeIncludeSearchPath>${xml:b.includes_search_path}</NMakeIncludeSearchPath>
			<NMakePreprocessorDefinitions>${xml:b.preprocessor_definitions};$(NMakePreprocessorDefinitions)</NMakePreprocessorDefinitions>
			<IncludePath>${xml:b.includes_search_path}</IncludePath>
			${if getattr(b, 'output_file', None)}
				<NMakeOutput>${xml:b.output_file}</NMakeOutput>
				<ExecutablePath>${xml:b.output_file}</ExecutablePath>
			${endif}
			${if not getattr(b, 'output_file', None)}
				<NMakeOutput>not_supported</NMakeOutput>
				<ExecutablePath>not_supported</ExecutablePath>
			${endif}
			${if getattr(b, 'output_file_name', None)}
				<TargetName>${b.output_file_name}</TargetName>
			${endif}
			${if getattr(b, 'deploy_dir', None)}
				<RemoteRoot>${xml:b.deploy_dir}</RemoteRoot>
			${endif}
			${if b.platform == 'Linux X64 GCC'}
				${if 'Debug' in b.configuration_name}
					<OutDir>${xml:project.get_output_folder('linux_x64_gcc','Debug')}</OutDir>
				${else}
					<OutDir>${xml:project.get_output_folder('linux_x64_gcc','')}</OutDir>
				${endif}
			%s
			${endif}
		</PropertyGroup>
	${endfor}
	${for b in project.build_properties}
		${if getattr(b, 'deploy_dir', None)}
			<ItemDefinitionGroup Condition="'$(Configuration)|$(Platform)'=='${b.configuration_name}|${b.platform.split()[0]}'">
				<Deploy>
					<DeploymentType>CopyToHardDrive</DeploymentType>
				</Deploy>
			</ItemDefinitionGroup>
		${endif}
	${endfor}
	<ItemGroup>
		${for x in project.source}
			<${project.get_key(x)} Include='${x.abspath()}' />
		${endfor}
	</ItemGroup>
	<Import Project="$(VCTargetsPath)\Microsoft.Cpp.targets" />
	<Import Project='${b.build_targets_path}' />
	<ImportGroup Label="ExtensionTargets" />
</Project>
'''

PROJECT_USER_TEMPLATE = '''<?xml version="1.0" encoding="UTF-8"?>
<Project ToolsVersion="4.0" xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
	${for b in project.build_properties}
		%s
	${endfor}
</Project>
'''

FILTER_TEMPLATE = '''<?xml version="1.0" encoding="UTF-8"?>
<Project ToolsVersion="4.0" xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
	<ItemGroup>
		${for x in project.source}
			<${project.get_key(x)} Include="${x.abspath()}">
			${if project.get_filter_name(x) != '.'}
				<Filter>${project.get_filter_name(x)}</Filter>
			${endif}
			</${project.get_key(x)}>
		${endfor}
	</ItemGroup>
	<ItemGroup>
		${for x in project.dirs()}
			<Filter Include="${x}">
				<UniqueIdentifier>{${project.make_uuid(x)}}</UniqueIdentifier>
			</Filter>
		${endfor}
	</ItemGroup>
</Project>
'''

# Note:
# Please mirror any changes in the solution template in the msvs_override_handling.py | get_solution_overrides()
# This also include format changes such as spaces

SOLUTION_TEMPLATE = '''Microsoft Visual Studio Solution File, Format Version ${project.formatver}
# Visual Studio ${project.vsver}
${for p in project.all_projects}
Project("{${p.ptype()}}") = "${p.name}", "${p.title}", "{${p.uuid}}"
	${if p != project.waf_project}
		ProjectSection(ProjectDependencies) = postProject
			{${project.waf_project.uuid}} = {${project.waf_project.uuid}}
		EndProjectSection
	${endif}
EndProject${endfor}
Global
	GlobalSection(SolutionConfigurationPlatforms) = preSolution
		${if project.all_projects}
			${for (configuration, platform) in project.all_projects[0].ctx.project_configurations()}
				${configuration}|${platform.split()[0]} = ${configuration}|${platform.split()[0]}
			${endfor}
		${endif}
	EndGlobalSection
	GlobalSection(ProjectConfigurationPlatforms) = postSolution
		${for p in project.all_projects}
			${if hasattr(p, 'source')}
				${for b in p.build_properties}
					{${p.uuid}}.${b.configuration_name}|${b.platform.split()[0]}.ActiveCfg = ${b.configuration_name}|${b.platform.split()[0]}
					${if getattr(p, 'is_active', None)}
						{${p.uuid}}.${b.configuration_name}|${b.platform.split()[0]}.Build.0 = ${b.configuration_name}|${b.platform.split()[0]}
					${endif}
					${if getattr(p, 'is_deploy', None) and b.platform.lower() in p.is_deploy}
						{${p.uuid}}.${b.configuration_name}|${b.platform.split()[0]}.Deploy.0 = ${b.configuration_name}|${b.platform.split()[0]}
					${endif}
				${endfor}
			${endif}
		${endfor}
	EndGlobalSection
	GlobalSection(SolutionProperties) = preSolution
		HideSolutionNode = FALSE
	EndGlobalSection
	GlobalSection(NestedProjects) = preSolution
	${for p in project.all_projects}
		${if p.parent}
			{${p.uuid}} = {${p.parent.uuid}}
		${endif}
	${endfor}
	EndGlobalSection
EndGlobal
'''

PROPERTY_SHEET_TEMPLATE = r'''<?xml version="1.0" encoding="UTF-8"?>
<Project DefaultTargets="Build" ToolsVersion="4.0"
	xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
	${for b in project.build_properties}	
		${if getattr(b, 'output_file_name', None)}
			<PropertyGroup Condition="'$(Configuration)|$(Platform)'=='${b.configuration_name}|${b.platform.split()[0]}'">
				<WAF_ExProjectTool>${xml:b.is_external_project_tool}</WAF_ExProjectTool>
				<WAF_TargetFile>${xml:b.ld_target_path}</WAF_TargetFile>
				<TargetPath Condition="'$(WAF_TargetFile)' != ''">$(WAF_TargetFile)</TargetPath>
				<LocalDebuggerCommand>${xml:b.ld_target_command}</LocalDebuggerCommand>
				<LocalDebuggerCommand Condition="$(TargetPath.EndsWith('.dll')) And  $(Configuration.EndsWith('_test'))">$(BinDir)/AzTestRunner</LocalDebuggerCommand>
				<LocalDebuggerCommandArguments Condition="$(WAF_ExProjectTool.Equals('True'))">${xml:b.ld_target_arguments}</LocalDebuggerCommandArguments>
				<LocalDebuggerCommandArguments Condition="$(TargetPath.EndsWith('.dll')) And$(Configuration.EndsWith('_test'))">"${xml:b.output_file}" AzRunUnitTests --pause-on-completion --gtest_break_on_failure</LocalDebuggerCommandArguments>
				<LocalDebuggerWorkingDirectory>$(OutDir)</LocalDebuggerWorkingDirectory>
			</PropertyGroup>
		${endif}
		<ItemDefinitionGroup Condition="'$(Configuration)|$(Platform)'=='${b.configuration_name}|${b.platform.split()[0]}'">
			<ClCompile>
				<WAF_SingleCompilationMode>Code</WAF_SingleCompilationMode>
				<WAF_TargetSolution>${xml:project.ctx.get_solution_node().abspath()} </WAF_TargetSolution>
				${if getattr(b, 'target_spec', None)}
					<WAF_TargetSpec>${xml:b.target_spec}</WAF_TargetSpec>
				${endif}
				${if getattr(b, 'target_config', None)}
					<WAF_TargetConfig>${xml:b.target_config}</WAF_TargetConfig>
				${endif}
				${if getattr(b, 'output_file_name', None)}
					<WAF_TargetName>${xml:b.output_file_name}</WAF_TargetName>
				${endif}
				${if getattr(b, 'output_file', None)}
					<WAF_TargetFile>${xml:b.output_file} </WAF_TargetFile>
				${endif}
				${if getattr(b, 'includes_search_path', None)}
					<WAF_IncludeDirectories>${xml:b.includes_search_path}</WAF_IncludeDirectories>
				${endif}
				${if getattr(b, 'preprocessor_definitions', None)}
					<WAF_PreprocessorDefinitions>${xml:b.preprocessor_definitions}</WAF_PreprocessorDefinitions>
				${endif}
				${if getattr(b, 'deploy_dir', None)}
					<WAF_DeployDir>${xml:b.deploy_dir}</WAF_DeployDir>
				${endif}
				${if getattr(b, 'output_dir', None)}
					<WAF_OutputDir>${xml:b.output_dir}</WAF_OutputDir>
				${endif}
				${if getattr(b, 'c_flags', None)}
					<WAF_CompilerOptions_C>${xml:b.c_flags} </WAF_CompilerOptions_C>
				${endif}
				${if getattr(b, 'cxx_flags', None)}
					<WAF_CompilerOptions_CXX>${xml:b.cxx_flags} </WAF_CompilerOptions_CXX>
				${endif}
				${if getattr(b, 'link_flags', None)}
				<WAF_LinkerOptions>${xml:b.link_flags}</WAF_LinkerOptions>
				${endif}
				<WAF_DisableCompilerOptimization>false</WAF_DisableCompilerOptimization>
				<WAF_ExcludeFromUberFile>false</WAF_ExcludeFromUberFile>
				<WAF_BuildCommandLine>${xml:project.get_build_command(b)}</WAF_BuildCommandLine>
				<WAF_RebuildCommandLine>${xml:project.get_rebuild_command(b)}</WAF_RebuildCommandLine>
				<WAF_CleanCommandLine>${xml:project.get_clean_command(b)}</WAF_CleanCommandLine>
				${if getattr(b, 'layout_dir', None)}
					<LayoutDir>${xml:b.layout_dir}</LayoutDir>
				${elif getattr(b, 'deploy_dir', None)}}
					<LayoutDir>${xml:b.deploy_dir}</LayoutDir>
				${endif}
				<LayoutExtensionFilter>*</LayoutExtensionFilter>
			</ClCompile>
			<Deploy>
				<DeploymentType>CopyToHardDrive</DeploymentType>
			</Deploy>
		</ItemDefinitionGroup>
	${endfor}
	<ItemDefinitionGroup />
	<ItemGroup />
</Project>
'''

COMPILE_TEMPLATE = '''def f(project):
	lst = []
	def xml_escape(value):
		return value.replace("&", "&amp;").replace('"', "&quot;").replace("'", "&apos;").replace("<", "&lt;").replace(">", "&gt;")

	%s
	
	return ''.join(lst)
'''


class VS_Configuration(object):

    def __init__(self, vs_spec, waf_spec, waf_configuration):
        self.vs_spec = vs_spec
        self.waf_spec = waf_spec
        self.waf_configuration = waf_configuration

    def __str__(self):
        return '[{}] {}'.format(self.vs_spec, self.waf_configuration)

    def __hash__(self):
        return hash((self.vs_spec, self.waf_spec, self.waf_configuration))

    def __eq__(self, other):
        if not isinstance(other, type(self)): return NotImplemented
        return self.vs_spec == other.vs_spec and self.waf_spec == other.waf_spec and self.waf_configuration == other.waf_configuration

    def __lt__(self, other):
         return self.__str__() < other.__str__()


@conf
def convert_vs_platform_to_waf_platform(self, vs_platform):
    try:
        return self.vs_platform_to_waf_platform[vs_platform]
    except KeyError:
        raise Errors.WafError("Invalid VS platform '{}'".format(vs_platform))


@conf
def is_valid_spec(ctx, spec_name):
    """ Check if the spec should be included when generating visual studio projects """
    if ctx.options.specs_to_include_in_project_generation == '':
        return True

    allowed_specs = ctx.options.specs_to_include_in_project_generation.replace(' ', '').split(',')
    if spec_name in allowed_specs:
        return True

    return False


@conf
def convert_waf_spec_to_vs_spec(self, spec_name):
    """ Helper to get the Visual Studio Spec name from a WAF spec name """
    return self.spec_visual_studio_name(spec_name)

reg_act = re.compile(r"(?P<backslash>\\)|(?P<dollar>\$\$)|(?P<subst>\$\{(?P<code>[^}]*?)\})", re.M)


def compile_template(line):
    """
    Compile a template expression into a python function (like jsps, but way shorter)
    """
    extr = []

    def repl(match):
        g = match.group
        if g('dollar'): return "$"
        elif g('backslash'):
            return "\\"
        elif g('subst'):
            extr.append(g('code'))
            return "<<|@|>>"
        return None

    line2 = reg_act.sub(repl, line)
    params = line2.split('<<|@|>>')
    assert(extr)

    indent = 0
    buf = []
    app = buf.append

    def app(txt):
        buf.append(indent * '\t' + txt)

    for x in range(len(extr)):
        if params[x]:
            app("lst.append(%r)" % params[x])

        f = extr[x]
        if f.startswith('if') or f.startswith('for'):
            app(f + ':')
            indent += 1
        elif f.startswith('py:'):
            app(f[3:])
        elif f.startswith('endif') or f.startswith('endfor'):
            indent -= 1
        elif f.startswith('else') or f.startswith('elif'):
            indent -= 1
            app(f + ':')
            indent += 1
        elif f.startswith('xml:'):
            app('lst.append(xml_escape(%s))' % f[4:])
        else:
            app('lst.append(%s)' % f)

    if extr:
        if params[-1]:
            app("lst.append(%r)" % params[-1])

    fun = COMPILE_TEMPLATE % "\n\t".join(buf)

    return Task.funex(fun)


def rm_blank_lines(txt):
    txt = "\r\n".join([line for line in txt.splitlines() if line.strip() != ''])
    txt += '\r\n'
    return txt


BOM = '\xef\xbb\xbf'
try:
    BOM = bytes(BOM, 'iso8859-1') # python 3
except:
    pass


def stealth_write(self, data, flags='wb'):
    try:
        unicode
    except NameError:
        data = data.encode('utf-8') # python 3
    else:
        data = data.decode(sys.getfilesystemencoding(), 'replace')
        data = data.encode('utf-8')

    if self.name.endswith('.vcproj') or self.name.endswith('.vcxproj'):
        data = BOM + data

    try:
        txt = self.read(flags='rb')
        if txt != data:
            raise ValueError('must write')
    except (IOError, ValueError):
        self.write(data, flags=flags)
    else:
        Logs.debug('msvs: skipping %s' % self.abspath())


Node.Node.stealth_write = stealth_write

re_quote = re.compile("[^a-zA-Z0-9-.]")


def quote(s):
    return re_quote.sub("_", s)


def xml_escape(value):
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("'", "&apos;").replace("<", "&lt;").replace(">", "&gt;")

def make_uuid(v, prefix = None):
    """
    simple utility function
    """
    if isinstance(v, dict):
        keys = list(v.keys())
        keys.sort()
        tmp = str([(k, v[k]) for k in keys])
    else:
        tmp = str(v)
    d = Utils.md5(tmp.encode()).hexdigest().upper()
    if prefix:
        d = '%s%s' % (prefix, d[8:])
    gid = uuid.UUID(d, version = 4)
    return str(gid).upper()

def diff(node, fromnode):
    # difference between two nodes, but with "(..)" instead of ".."
    c1 = node
    c2 = fromnode

    c1h = c1.height()
    c2h = c2.height()

    lst = []
    up = 0

    while c1h > c2h:
        lst.append(c1.name)
        c1 = c1.parent
        c1h -= 1

    while c2h > c1h:
        up += 1
        c2 = c2.parent
        c2h -= 1

    while id(c1) != id(c2):
        lst.append(c1.name)
        up += 1

        c1 = c1.parent
        c2 = c2.parent

    for i in range(up):
        lst.append('(..)')
    lst.reverse()
    return tuple(lst)


class build_property(object):
    pass


class vsnode(object):
    """
    Abstract class representing visual studio elements
    We assume that all visual studio nodes have a uuid and a parent
    """

    def __init__(self, ctx):
        self.ctx = ctx # msvs context
        self.name = '' # string, mandatory
        self.vspath = '' # path in visual studio (name for dirs, absolute path for projects)
        self.uuid = '' # string, mandatory
        self.parent = None # parent node for visual studio nesting

    def get_waf(self):
        """
        Override in subclasses...
        """
        return 'cd /d "%s" & "%s"' % (self.ctx.srcnode.abspath(), getattr(self.ctx, 'waf_command', cry_utils.WAF_EXECUTABLE))

    def ptype(self):
        """
        Return a special uuid for projects written in the solution file
        """
        pass

    def write(self):
        """
        Write the project file, by default, do nothing
        """
        pass

    def make_uuid(self, val):
        """
        Alias for creating uuid values easily (the templates cannot access global variables)
        """
        return make_uuid(val)


class vsnode_vsdir(vsnode):
    """
    Nodes representing visual studio folders (which do not match the filesystem tree!)
    """
    VS_GUID_SOLUTIONFOLDER = "2150E333-8FDC-42A3-9474-1A3956D46DE8"

    def __init__(self, ctx, uuid, name, vspath=''):
        vsnode.__init__(self, ctx)
        self.title = self.name = name
        self.uuid = uuid
        self.vspath = vspath or name

    def ptype(self):
        return self.VS_GUID_SOLUTIONFOLDER


def _get_filter_name(project_filter_dict, file_name):
    """ Helper function to get a correct project filter """
    if file_name.endswith('wscript') or file_name.endswith('.waf_files'):
        return '_WAF_' # Special case for wscript files

    if not file_name in project_filter_dict:
        return 'FILE_NOT_FOUND'

    project_filter = project_filter_dict[file_name]
    project_filter = project_filter.replace('/', '\\')
    if project_filter.lower() == 'root':
        return '.'

    return project_filter


class vsnode_project(vsnode):
    """
    Abstract class representing visual studio project elements
    A project is assumed to be writable, and has a node representing the file to write to
    """
    VS_GUID_VCPROJ = "8BC9CEB8-8B4A-11D0-8D11-00A0C91BC942"
    def ptype(self):
        return self.VS_GUID_VCPROJ

    def __init__(self, ctx, node):
        vsnode.__init__(self, ctx)
        self.path = node
        self.uuid = make_uuid(node.abspath())
        self.name = node.name
        self.title = self.path.abspath()
        self.source = [] # list of node objects
        self.build_properties = [] # list of properties (nmake commands, output dir, etc)

        self.compat_toolsets = ctx.compat_toolsets
        self.defaultPlatformToolSet = ctx.defaultPlatformToolSet
        self.msvs_version = ctx.msvs_version

        canonical_enabled_game_list = ','.join(split_comma_delimited_string(ctx.options.enabled_game_projects, True))
        self.enabled_game_projects = canonical_enabled_game_list

    def get_platform_toolset(self, vs_platform):

        platform = self.get_platform_for_toolset_name(vs_platform)
        if platform:
            compat_toolsets = platform.attributes['msvs']['compat_toolsets']

            for compat_toolset in compat_toolsets:
                if compat_toolset in self.compat_toolsets:
                    return compat_toolset

        # If we reach this point, that means that the toolset for platform specified in the vs platform dictionary
        # cannot be supported by the current VS ide.  It should not reach this point because the project should
        # be filter out, but log a warning anyways and continue
        Logs.warn('Invalid platform error for vs platform {}:  Platform not found, defaulting to {}'.format(vs_platform,
                                                                                                            self.defaultPlatformToolSet))
        return self.defaultPlatformToolSet

    def get_platform_for_toolset_name(self, toolset_name):
        for platform in self.ctx.toolset_map[toolset_name]:
            # Filter out platform toolsets that don't match the msvs generator msvs_version value
            msvs_version_list = platform.attributes['msvs']['msvs_ver']
            if not msvs_version_list:
                continue
            msvs_version_list = [msvs_version_list] if isinstance(msvs_version_list, str) else msvs_version_list
            if self.msvs_version in msvs_version_list:
                return platform
        return None

    def dirs(self):
        """
        Get the list of parent folders of the source files (header files included)
        for writing the filters
        """
        lst = []

        def add(x):
            if not x.lower() == '.':
                lst.append(x)

            seperator_pos = x.rfind('\\')
            if seperator_pos != -1:
                add(x[:seperator_pos])

        for source in self.source:
            add( _get_filter_name(self.project_filter, source.abspath()) )

        # Remove duplicates
        lst = list(set(lst))
        return lst

    def write(self):
        Logs.debug('msvs: creating %r' % self.path)

        def _write(template_name, output_node):
            template = compile_template(template_name)
            template_str = template(self)
            template_str = rm_blank_lines(template_str)
            output_node.stealth_write(template_str)
            
        restricted_property_group_sections = self.ctx.msvs_restricted_property_group_section()
        restricted_property_group_section_str = '\n'.join(restricted_property_group_sections).strip()

        restricted_user_template_sections = self.ctx.msvs_restricted_user_template_section()
        restricted_user_template_sections_str = '\n'.join(restricted_user_template_sections).strip()

        _write(PROJECT_TEMPLATE % (restricted_property_group_section_str), self.path)
        _write(FILTER_TEMPLATE, self.path.parent.make_node(self.path.name + '.filters'))
        _write(PROJECT_USER_TEMPLATE % (restricted_user_template_sections_str), self.path.parent.make_node(self.path.name + '.user'))
        _write(PROPERTY_SHEET_TEMPLATE, self.path.parent.make_node(self.path.name + '.default.props'))

    def get_key(self, node):
        """
        required for writing the source files
        """
        name = node.name
        if self.ctx.is_cxx_file(name):
            return 'ClCompile'
        return 'ClInclude'

    def collect_properties(self):
        """
        Returns a list of triplet (configuration, platform, output_directory)
        """
        ret = []

        for vs_configuration in self.ctx.configurations:

            for platform in list(self.ctx.platform_map.values()):

                toolset_name = platform.attributes['msvs']['toolset_name']

                if not self.get_platform_for_toolset_name(toolset_name):
                    continue

                x = build_property()
                x.outdir = ''
                x.bindir = ''
                x.configuration = vs_configuration
                x.configuration_name = str(vs_configuration)
                x.platform = toolset_name

                active_projects = self.ctx.get_enabled_game_project_list()
                if len(active_projects) != 1:
                    x.game_project = ''
                else:
                    x.game_project = active_projects[0]

                x.preprocessor_definitions = ''
                x.includes_search_path = ''
                x.build_targets_path = self.ctx.get_engine_node().make_node(['_WAF_', 'msbuild', 'waf_build.targets']).abspath()

                ret.append(x)
        self.build_properties = ret

    def get_build_params(self, props):

        opt = '--execsolution="{}"'.format(self.ctx.get_solution_node().abspath()) + \
              ' --msvs-version={}'.format(self.msvs_version) + \
              ' --enabled-game-projects={}'.format(self.enabled_game_projects) + \
              ' --project-spec={}'.format(props.configuration.waf_spec)

        return self.get_waf(), opt

    def get_build_command(self, props):

        params = self.get_build_params(props)

        platform = self.get_platform_for_toolset_name(props.platform)
        if not platform:
            Logs.error('[ERROR] build command: Toolset name {} does not have a msvs_ver entry which matches the value of {}.'.format(props.platform, self.msvs_version))
            return None

        waf_platform = platform.platform
        waf_configuration = props.configuration.waf_configuration

        bld_command = ' build_{}_{}'.format(waf_platform, waf_configuration)

        return '{} {} {}'.format(params[0], bld_command, params[1])

    def get_clean_command(self, props):

        params = self.get_build_params(props)

        platform = self.get_platform_for_toolset_name(props.platform)
        if not platform:
            Logs.error('[ERROR] clean command: Toolset name {} does not have a msvs_ver entry which matches the value of {}.'.format(props.platform, self.msvs_version))
            return None

        waf_platform = platform.platform

        waf_configuration = props.configuration.waf_configuration

        clean_command = ' clean_{}_{}'.format(waf_platform, waf_configuration)

        return '{} {} {}'.format(params[0], clean_command, params[1])

    def get_output_folder(self, platform, configuration):
        output_folders = self.ctx.get_output_folders(platform, configuration)
        if len(output_folders) == 0:
            return ''

        output_path = output_folders[0]
        relative_path = output_path.path_from(self.ctx.path)

        return '$(SolutionDir)..\\' + relative_path + '\\'

    def get_rebuild_command(self, props):

        params = self.get_build_params(props)

        platform = self.get_platform_for_toolset_name(props.platform)
        if not platform:
            Logs.error('[ERROR] rebuild command: Toolset name {} does not have a msvs_ver entry which matches the value of {}.'.format(props.platform, self.msvs_version))
            return None

        waf_platform = platform.platform

        waf_configuration = props.configuration.waf_configuration

        clean_command = ' clean_{}_{}'.format(waf_platform, waf_configuration)
        bld_command = ' build_{}_{}'.format(waf_platform, waf_configuration)

        return '{} {} {} {}'.format(params[0], clean_command, bld_command, params[1])

    def get_filter_name(self, node):
        return _get_filter_name(self.project_filter, node.abspath())

    def get_all_config_platform_conditional(self):
        condition_string = " Or ".join(
            ["('$(Configuration)|$(Platform)'=='%s|%s')" % (prop.configuration, prop.platform) for prop in
             self.build_properties])
        return condition_string

    def get_all_config_platform_conditional_trimmed(self):
        condition_string = " Or ".join(
            ["('$(Configuration)|$(Platform)'=='%s|%s')" % (prop.configuration, prop.platform.split()[0]) for prop in
             self.build_properties])
        return condition_string


class vsnode_alias(vsnode_project):
    def __init__(self, ctx, node, name):
        vsnode_project.__init__(self, ctx, node)
        self.name = name
        self.output_file = ''
        self.project_filter = {}


class vsnode_build_all(vsnode_alias):
    """
    Fake target used to emulate the behaviour of "make all" (starting one process by target is slow)
    This is the only alias enabled by default
    """

    def __init__(self, ctx, node, name='_WAF_'):
        vsnode_alias.__init__(self, ctx, node, name)
        self.is_active = True

    def collect_properties(self):
        """
        Visual studio projects are associated with platforms and configurations (for building especially)
        """
        super(vsnode_build_all, self).collect_properties()

        for x in self.build_properties:
            x.outdir = self.path.parent.abspath()
            x.bindir = self.path.parent.abspath()
            x.preprocessor_definitions = ''
            x.includes_search_path = ''
            x.c_flags = ''
            x.cxx_flags = ''
            x.link_flags = ''
            x.target_spec = ''
            x.target_config = ''

            current_spec_name = x.configuration.vs_spec

            # If the toolset map list doesn't contain a platform which matches this node msvs_version, it is skipped
            platform = self.get_platform_for_toolset_name(x.platform)
            if not platform:
                continue

            waf_platform = platform.platform
            waf_configuration = x.configuration.waf_configuration

            x.target_spec = current_spec_name
            x.target_config = waf_platform + '_' + waf_configuration

        # Collect WAF files
        search_roots = self.ctx._get_settings_search_roots()

        config_files = list()
        spec_files = dict()
        for root in search_roots:
            waf_config_dir = root.make_node('_WAF_')
            if os.path.exists(waf_config_dir.abspath()):
                config_file_nodes = waf_config_dir.ant_glob('**/*.json', maxdepth=1, excl=['3rdParty', 'msbuild', 'specs', 'settings', '*.options'])
                config_files.extend(config_file_nodes)

                spec_dir = waf_config_dir.make_node('specs')
                if os.path.exists(spec_dir.abspath()):
                    spec_file_nodes = spec_dir.ant_glob('*.json')
                    spec_files.update({ node.name : node for node in spec_file_nodes })

        waf_config_files = config_files + [ search_roots[-1].make_node(['_WAF_', 'user_settings.options']) ] # index '-1' is guaranteed to be launch dir (project)
        waf_spec_files = list(spec_files.values())

        waf_source_dir = self.ctx.engine_node.make_node('Tools/build/waf-1.7.13')
        waf_scripts = waf_source_dir.make_node('waflib').ant_glob('**/*.py')
        waf_lumberyard_scripts = waf_source_dir.make_node('lmbrwaflib').ant_glob('**/*.py')

        # Remove files found in lmbrwaflib from waf scripts
        tmp = []
        waf_lumberyard_scripts_files = []
        for node in waf_lumberyard_scripts:
            waf_lumberyard_scripts_files += [os.path.basename(node.abspath())]

        for x in waf_scripts:
            if os.path.basename(x.abspath()) in waf_lumberyard_scripts_files:
                continue
            tmp += [x]
        waf_scripts = tmp

        for file in waf_config_files:
            self.project_filter[file.abspath()] = 'Settings'
        for file in waf_spec_files:
            self.project_filter[file.abspath()] = 'Specs'

        for file in waf_scripts:
            filter_name = 'Scripts'
            subdir = os.path.dirname(file.path_from(waf_source_dir.make_node('waflib')))
            if subdir != '':
                filter_name += '/' + subdir
            self.project_filter[file.abspath()] = filter_name

        for file in waf_lumberyard_scripts:
            self.project_filter[file.abspath()] = 'Lumberyard Scripts'

        self.source += waf_config_files + waf_spec_files + waf_scripts + waf_lumberyard_scripts

        # without a cpp file, VS wont compile this project
        dummy_node = self.ctx.get_bintemp_folder_node().make_node('__waf_compile_dummy__.cpp')
        self.project_filter[dummy_node.abspath()] = 'DummyCompileNode'
        self.source += [dummy_node]


class vsnode_install_all(vsnode_alias):
    """
    Fake target used to emulate the behaviour of "make install"
    """

    def __init__(self, ctx, node, name='install_all_projects'):
        vsnode_alias.__init__(self, ctx, node, name)

    def get_build_command(self, props):
        return "%s build install %s" % self.get_build_params(props)

    def get_clean_command(self, props):
        return "%s clean %s" % self.get_build_params(props)

    def get_rebuild_command(self, props):
        return "%s clean build install %s" % self.get_build_params(props)


class vsnode_project_view(vsnode_alias):
    """
    Fake target used to emulate a file system view
    """

    def __init__(self, ctx, node, name='project_view'):
        vsnode_alias.__init__(self, ctx, node, name)
        self.tg = self.ctx()  # fake one, cannot remove
        self.exclude_files = Node.exclude_regs + '''
waf-1.7.*
waf3-1.7.*/**
.waf-1.7.*
.waf3-1.7.*/**
**/*.sdf
**/*.suo
**/*.ncb
**/%s
        ''' % Options.lockfile

    def collect_source(self):
        # this is likely to be slow
        self.source = self.ctx.srcnode.ant_glob('**', excl=self.exclude_files)

    def get_build_command(self, props):
        params = self.get_build_params(props) + (self.ctx.cmd,)
        return "%s %s %s" % params

    def get_clean_command(self, props):
        return ""

    def get_rebuild_command(self, props):
        return self.get_build_command(props)


class vsnode_target(vsnode_project):
    """
    Visual studio project representing a targets (programs, libraries, etc) and bound
    to a task generator
    """

    def __init__(self, ctx, tg):
        """
        A project is more or less equivalent to a file/folder
        """
        base = getattr(ctx, 'projects_dir', None) or tg.path
        node = base.make_node(quote(tg.name) + ctx.project_extension) # the project file as a Node
        self.ctx = ctx
        vsnode_project.__init__(self, ctx, node)
        self.name = quote(tg.name)
        self.tg = tg  # task generator
        if getattr(tg, 'need_deploy', None):
            if not isinstance(tg.need_deploy, list):
                self.is_deploy = [tg.need_deploy]
            else:
                self.is_deploy = tg.need_deploy
        self.project_filter = self.tg.project_filter
        self.engine_external = not ctx.is_engine_local()

    def get_build_params(self, props):
        """
        Override the default to add the target name
        """
        opt = '--execsolution="%s"' % self.ctx.get_solution_node().abspath()

        opt += ' --msvs-version={}'.format(self.msvs_version)
        opt += ' --project-spec={}'.format(props.configuration.waf_spec)

        if getattr(self, 'tg', None):
            opt += " --targets=%s" % self.tg.name
        # Propagate verbosity to vc proj build commands
        if self.ctx.options.verbose:
            opt += " -"
            for i in range(self.ctx.options.verbose):
                opt += 'v'
        return (self.get_waf(), opt)

    def collect_source(self):
        tg = self.tg
        source_files = tg.to_nodes(getattr(tg, 'source', []))
        include_dirs = Utils.to_list(getattr(tg, 'msvs_includes', []))
        waf_file_entries = self.tg.waf_file_entries;

        include_files = []
        """"
        for x in include_dirs:
            if isinstance(x, str):
                x = tg.path.find_node(x)
            if x:
                lst = [y for y in x.ant_glob(HEADERS_GLOB, flat=False)]
                include_files.extend(lst)
        """
        # remove duplicates
        self.source.extend(list(set(source_files + include_files)))
        self.source += [tg.path.make_node('wscript')] + waf_file_entries
        self.source.sort(key=lambda x: x.abspath())

    def ConvertToDict(self, var):
        # dict type
        if isinstance(var, dict):
            return dict(var)

        # list type
        if isinstance(var, list):
            return dict(var)

        # class type
        try:
            return dict(var.__dict__)
        except:
            return None

    def GetPlatformSettings(self, target_platform, target_configuration, name, settings):
        """
        Util function to apply flags based on current platform
        """

        platform_details = self.ctx.get_target_platform_detail(target_platform)
        config_details = platform_details.get_configuration(target_configuration)
        platforms = [platform_details.platform] + list(platform_details.aliases)

        result = []

        settings_dict = self.ConvertToDict(settings)

        if not settings_dict:
            print(("WAF_ERROR: Unsupported type '%s' for 'settings' variable encountered." % type(settings)))
            return

        # add non platform specific settings
        try:
            if isinstance(settings_dict[name], list):
                result += settings_dict[name]
            else:
                result += [settings_dict[name]]
        except:
            pass

        # add per configuration flags
        configuration_specific_name = (target_configuration + '_' + name)
        try:
            if isinstance(settings_dict[configuration_specific_name], list):
                result += settings_dict[configuration_specific_name]
            else:
                result += [settings_dict[configuration_specific_name]]
        except:
            pass

        # add per platform flags
        for platform in platforms:
            platform_specific_name = (platform + '_' + name)
            try:
                if isinstance(settings_dict[platform_specific_name], list):
                    result += settings_dict[platform_specific_name]
                else:
                    result += [settings_dict[platform_specific_name]]
            except:
                pass

        # add per platform_configuration flags
        for platform in platforms:
            platform_configuration_specific_name = (platform + '_' + target_configuration + '_' + name)
            try:
                if isinstance(settings_dict[platform_configuration_specific_name], list):
                    result += settings_dict[platform_configuration_specific_name]
                else:
                    result += [settings_dict[platform_configuration_specific_name]]
            except:
                pass

        return result

    def get_platform_settings_from_project_settings(self, base_project_path, config_details, names):

        filter_names = names + ['project_settings']

        def _convert_tg_to_kw_dict():
            result = {}

            for key, value in list(self.tg.__dict__.items()):
                if key in filter_names:
                    result[key] = value
            return result

        result_dict = _convert_tg_to_kw_dict()

        additional_alias = {
            'TARGET': self.name,
            'OUTPUT_FILENAME': getattr(self.tg, 'output_file_name', self.name)
        }

        apply_project_settings_for_input(ctx=self.ctx,
                                         configuration=config_details,
                                         target=self.name,
                                         kw=result_dict,
                                         additional_aliases=additional_alias,
                                         local_settings_base_path=base_project_path,
                                         filter_keywords=filter_names)

        return result_dict

    # Method to recurse a taskgen item's use dependencies and build up a unique include_list for include paths
    def recurse_use_includes_and_defines(self, waf_platform, waf_configuration, cur_tg, include_list, defines_list, uselib_cache):
        """
        Method to recurse a taskgen item's use dependencies and build up a unique include_list for include paths

        :param waf_platform:    The current waf_platform
        :param cur_tg:          The taskgen to inspect
        :param include_list:    The includes list to populate
        :param defines_list:    The defines list to populate
        :param uselib_cache:    uselib cache to maintain to prevent unnecessary processing
        """
        visited_nodes = set()

        def _recurse(tg):

            # Prevent infinite loops from possible cyclic dependencies
            visited_nodes.add(tg.name)

            # Make sure that a list of strings is provided as the export includes
            check_export_includes = Utils.to_list(getattr(tg, 'export_includes', []))

            # Make sure the export includes is not empty before processing
            if check_export_includes:
                for raw_export_include in check_export_includes:
                    if (isinstance(raw_export_include, Node.Node)):
                        abs_export_include_path = raw_export_include.abspath()
                        append_to_unique_list(include_list, abs_export_include_path)
                    elif os.path.isabs(raw_export_include):
                        append_to_unique_list(include_list, raw_export_include)
                    else:
                        abs_export_include_path = tg.path.make_node(raw_export_include).abspath()
                        append_to_unique_list(include_list, abs_export_include_path)

            # Include export defines if provided
            if hasattr(tg, 'export_defines'):
                check_export_defines = Utils.to_list(tg.export_defines)
                if len(check_export_defines) > 0:
                    for export_define in check_export_defines:
                        append_to_unique_list(defines_list, export_define)

            # Perform additional includes and defines analysis on uselibs
            self.process_uselib_include_and_defines(waf_platform, waf_configuration, cur_tg, include_list, defines_list,uselib_cache)

            # Recurse into the 'use' for this taskgen if possible
            if not hasattr(tg, 'use') or len(tg.use) == 0:
                return
            for use_module in tg.use:
                if use_module in self.ctx.task_gen_cache_names:
                    use_tg = self.ctx.task_gen_cache_names[use_module]
                    if use_tg.name not in visited_nodes:
                        _recurse(use_tg)
                        visited_nodes.add(use_tg.name)

        _recurse(cur_tg)

    def process_uselib_include_and_defines(self, waf_platform, waf_configuration, cur_tg, include_list, defines_list, uselib_cache):
        """
        Perform inspection of a taskgen's uselib for additional includes and defines

        :param waf_platform:    The current waf_platform
        :param cur_tg:          The taskgen to inspect
        :param include_list:    The includes list to populate
        :param defines_list:    The defines list to populate
        :param uselib_cache:    uselib cache to maintain to prevent unnecessary processing
        """
        
        # Build up the  possible 'uselibs' based on the different possible variations of the kw name
        # based on platforms, test_ aliases, etc. for windows hosts.
        
        cached_uselib_names_key = 'uselib_name_combos_{}_{}'.format(waf_platform, waf_configuration)
        uselib_names = getattr(self, cached_uselib_names_key, None)
        if not uselib_names:
            uselib_names = ['uselib',
                            '{}_uselib'.format(waf_platform)]
    
            is_platform_windows = self.ctx.is_windows_platform(waf_platform)
            
            is_test_configuration = waf_configuration.endswith('_test')
            
            if is_platform_windows:
                uselib_names.extend(['win_uselib'])
            if is_test_configuration:
                # This is a test configuration. Correct the '_test' suffix of the configuration name
                # to its original form since the 'test_' aliases are all prefixes
                test_stripped_waf_configuration = waf_configuration[:len(waf_configuration) - 5]
                
                # Apply the possible 'test_' uselibs
                uselib_names.extend(['test_uselib',
                                     'test_all_uselib',
                                     'test_{}_uselib'.format(test_stripped_waf_configuration),
                                     'test_{}_uselib'.format(waf_platform),
                                     'test_{}_{}_uselib'.format(waf_platform, test_stripped_waf_configuration)])
                if is_platform_windows:
                    # If this is a windows platform, also apply the possible 'win' alias for test configurations
                    uselib_names.extend(['test_win_uselib',
                                         'test_win_{}_uselib'.format(test_stripped_waf_configuration)])
            else:
                # This is not a test configuration
                uselib_names.extend(['{}_{}_uselib'.format(waf_platform, waf_configuration)])
                if is_platform_windows:
                    # If this is a windows platform, also apply the possible 'win' alias
                    uselib_names.extend(['win_{}_uselib'.format(waf_configuration)])
            setattr(self, cached_uselib_names_key, uselib_names)

        tg_uselibs = []
        for uselib_name in uselib_names:
            tg_uselibs.extend(getattr(cur_tg, uselib_name, []))

        for tg_uselib in tg_uselibs:

            if tg_uselib in uselib_cache:
                # Get the cached includes value if any
                tg_uselib_includes_value = uselib_cache[tg_uselib][0]
                if tg_uselib_includes_value is not None and len(tg_uselib_includes_value) > 0:
                    append_to_unique_list(include_list, tg_uselib_includes_value)

                # Get the cached defines value if any
                tg_uselib_defines_value = uselib_cache[tg_uselib][1]
                if tg_uselib_defines_value is not None and len(tg_uselib_defines_value) > 0:
                    append_to_unique_list(defines_list, tg_uselib_defines_value)
            else:
                # Using the platform and configuration, get the env table to track down the includes and defines for the uselib name
                platform_config_key = waf_platform + '_' + waf_configuration
                platform_config_env = self.ctx.all_envs[platform_config_key]

                # Lookup the includes
                platform_config_uselib_includes_value = []
                includes_variable_name = 'INCLUDES_{}'.format(tg_uselib)
                includes_variable_name_debug = 'INCLUDES_{}D'.format(tg_uselib)
                system_includes_variable_name = 'SYSTEM_INCLUDES_{}'.format(tg_uselib)
                system_includes_variable_name_debug = 'SYSTEM_INCLUDES_{}D'.format(tg_uselib)

                if includes_variable_name in platform_config_env:
                    platform_config_uselib_includes_value = platform_config_env[includes_variable_name]
                elif includes_variable_name_debug in platform_config_env:
                    platform_config_uselib_includes_value = platform_config_env[includes_variable_name_debug]
                
                if system_includes_variable_name in platform_config_env:
                    platform_config_uselib_includes_value += platform_config_env[system_includes_variable_name]
                elif system_includes_variable_name_debug in platform_config_env:
                    platform_config_uselib_includes_value += platform_config_env[system_includes_variable_name_debug]

                if platform_config_uselib_includes_value:
                    append_to_unique_list(include_list, platform_config_uselib_includes_value)

                # Lookup the defines
                platform_config_uselib_defines_value = None
                defines_variable_name = 'DEFINES_{}'.format(tg_uselib)
                defines_variable_name_debug = 'DEFINES_{}D'.format(tg_uselib)

                if defines_variable_name in platform_config_env:
                    platform_config_uselib_defines_value = platform_config_env[defines_variable_name]
                elif defines_variable_name_debug in platform_config_env:
                    platform_config_uselib_defines_value = platform_config_env[defines_variable_name_debug]

                if platform_config_uselib_defines_value is not None:
                    append_to_unique_list(defines_list, platform_config_uselib_defines_value)

                # Cache the results
                uselib_cache[tg_uselib] = (platform_config_uselib_includes_value, platform_config_uselib_defines_value)

    def collect_properties(self):

        """
        Visual studio projects are associated with platforms and configurations (for building especially)
        """

        project_generator_node = self.ctx.bldnode.make_node('project_generator')
        project_generator_prefix = project_generator_node.abspath()
        bintemp_prefix = self.ctx.bldnode.abspath()

        super(vsnode_target, self).collect_properties()
        for x in self.build_properties:
            x.outdir = self.path.parent.abspath()
            x.bindir = self.path.parent.abspath()
            x.preprocessor_definitions = ''
            x.includes_search_path = ''
            x.c_flags = ''
            x.cxx_flags = ''
            x.link_flags = ''
            x.target_spec = ''
            x.target_config = ''

            x.is_external_project_tool = str(False)
            x.ld_target_path = ''
            x.ld_target_command = ''
            x.ld_target_arguments = ''

            if not hasattr(self.tg, 'link_task') and 'create_static_library' not in self.tg.features:
                continue

            current_spec_name = x.configuration.waf_spec

            # If the toolset map list doesn't contain a platform which matches this node msvs_version, it is skipped
            platform = self.get_platform_for_toolset_name(x.platform)
            if not platform:
                continue

            waf_platform = platform.platform
            waf_configuration = x.configuration.waf_configuration
            spec_modules = self.ctx.spec_modules(current_spec_name)
            waf_platform_config_key = waf_platform + '_' + waf_configuration

            include_project = True

            # Determine the module's supported platform(s) and configuration(s) and see if it needs to be included
            # in the project
            module_platforms = self.ctx.get_module_platforms(self.tg.target)
            module_configurations = self.ctx.get_module_configurations(self.tg.target, waf_platform)
            if waf_platform not in module_platforms:
                include_project = False
            elif waf_configuration not in module_configurations:
                include_project = False
            elif waf_platform_config_key not in self.ctx.all_envs:
                include_project = False
            else:
                target_name = self.tg.target
                all_module_uses = self.ctx.get_all_module_uses(current_spec_name, waf_platform, waf_configuration)
                unit_test_target = getattr(self.tg, 'unit_test_target', '')
                if (target_name not in spec_modules) and \
                        (target_name not in all_module_uses) and \
                        (unit_test_target not in spec_modules) and \
                        (unit_test_target not in all_module_uses):
                    include_project = False

            # Check if this project is supported for the current spec
            if not include_project:
                # Even if this project does is not supported by a particular configuration, it still needs a place-holder
                # for its value so MSVS will be able to load the project
                output_file_name = 'Unsupported_For_Configuration'
                x.output_file = output_file_name
                x.output_file_name = output_file_name
                x.includes_search_path = ""
                x.preprocessor_definitions = ""
                x.c_flags = ''
                x.cxx_flags = ''
                x.link_flags = ''
                x.target_spec = ''

                x.is_external_project_tool = str(False)
                x.ld_target_path = ''
                x.ld_target_command = ''
                x.ld_target_arguments = ''
                continue

            current_env = self.ctx.all_envs[waf_platform_config_key]
            msvc_helper.verify_options_common(current_env)
            x.c_flags = " ".join(current_env['CFLAGS'])  # convert list to space separated string
            x.cxx_flags = " ".join(current_env['CXXFLAGS'])  # convert list to space separated string
            x.link_flags = " ".join(current_env['LINKFLAGS'])  # convert list to space separated string

            x.target_spec = current_spec_name
            x.target_config = waf_platform + '_' + waf_configuration

            # Get the details of the platform+configuration for the current vs build configuration
            config_details = platform.get_configuration(waf_configuration)

            # If there is a new style project settings attribute, use the mechanism to extract from that data instead
            # of querying for platform+configuration built keywords
            project_settings_list = getattr(self.tg, 'project_settings', None)
            base_project_path = getattr(self.tg, 'base_project_settings_path', None)
            if project_settings_list:
                project_settings = self.get_platform_settings_from_project_settings(base_project_path,
                                                                                    config_details,
                                                                                    ['defines', 'includes', 'output_file_name'])
            else:
                project_settings = None

            has_link_task = False

            # For VS projects with a single game project target
            if hasattr(self.tg, 'link_task'):  # no link task implies special static modules which are static libraries
                link_task_pattern_name = self.tg.link_task.__class__.__name__ + '_PATTERN'
                has_link_task = True
            else:
                link_task_pattern_name = 'cxxstlib_PATTERN'

            pattern = current_env[link_task_pattern_name]

            output_folder_node = self.get_output_folder_node(waf_configuration, waf_platform)

            if project_settings:
                output_file_name = project_settings['output_file_name']
            else:
                # Legacy
                if waf_configuration.startswith('debug') and hasattr(self.tg, 'debug_output_file_name'):
                    output_file_name = self.tg.debug_output_file_name
                elif not waf_configuration.startswith('debug') and hasattr(self.tg, 'ndebug_output_file_name'):
                    output_file_name = self.tg.ndebug_output_file_name
                else:
                    output_file_name = self.tg.output_file_name

            # Save project info
            try:
                x.output_file = output_folder_node.abspath() + '\\' + pattern % output_file_name
            except TypeError:
                pass
            x.output_file_name = output_file_name
            x.output_path = os.path.dirname(x.output_file)
            x.outdir = x.output_path
            x.bindir = os.path.join(self.tg.bld.path.abspath(), self.tg.bld.get_output_target_folder(waf_platform, waf_configuration))
            x.assembly_name = output_file_name
            x.output_type = os.path.splitext(x.output_file)[1][1:]  # e.g. ".dll" to "dll"

            if not has_link_task:
                # Project with no outputs (static lib or pile of objects)
                # Even if this project does not have an executable, it still needs a place-holder
                # for its value so MSVS will be able to load the project
                output_file_name = ''
                x.output_file = output_file_name
                x.output_file_name = output_file_name

            # Collect all defines for this configuration
            define_list = list(current_env['DEFINES'])

            if project_settings:
                define_list += project_settings.get('defines', [])
            else:
                # Legacy
                define_list += self.GetPlatformSettings(waf_platform, waf_configuration, 'defines', self.tg)

            include_list = list(current_env['INCLUDES'])
            if project_settings:
                include_list += project_settings.get('includes', [])
            else:
                # Legacy
                include_list += self.GetPlatformSettings(waf_platform, waf_configuration, 'includes', self.tg)

            # make sure we only have absolute path for intellisense
            # If we don't have a absolute path, assume a relative one, hence prefix the path with the taskgen path and comvert it into an absolute one
            for i in range(len(include_list)):
                if (isinstance(include_list[i], Node.Node)):  # convert nodes to abs paths
                    include_list[i] = include_list[i].abspath()
                else:
                    if not os.path.isabs(include_list[i]):
                        include_list[i] = os.path.abspath(self.tg.path.abspath() + '/' + include_list[i])

            # Do a depth-first recursion into the use dependencies to collect any additional include paths
            uselib_include_cache = {}
            self.recurse_use_includes_and_defines(waf_platform, waf_configuration, self.tg, include_list, define_list, uselib_include_cache)

            # For generate header files that need to be included, the include path during project generation will
            # be $ROOT\BinTemp\project_generator\... because the task is generated based on files that are to be
            # created.  At this point, the include paths from this step will be included if they exist (ie UI4)
            # so we will filter and replace all paths that start with $ROOT\BinTemp\project_generator\... and
            # replace the header with what the files WILL BE during the build process, where those platform
            # specific header files will be generated
            platform_configuration = waf_platform + '_' + waf_configuration;
            replace_project_generator_prefix = self.ctx.bldnode.make_node(platform_configuration).abspath()
            include_list = [p.replace(project_generator_prefix, replace_project_generator_prefix) for p in include_list]
            replace_bintemp_to_target = self.ctx.bldnode.make_node(platform_configuration).abspath()
            include_list = [p.replace(bintemp_prefix, replace_bintemp_to_target) for p in include_list]

            if 'qt5' in self.tg.features:
                # Special case for projects that use QT.  It needs to resolve the intermediate/generated QT related files based
                # on the configuration.
                qt_intermediate_include_path = os.path.join(self.tg.bld.bldnode.abspath(),
                                                            x.target_config,
                                                            'qt5',
                                                            '{}.{}'.format(self.name, self.tg.target_uid))
                include_list.append(qt_intermediate_include_path)

            x.includes_search_path = ';'.join(include_list)
            x.preprocessor_definitions = ';'.join(define_list)

            x.ld_target_command = "$(TargetPath)"
            x.ld_target_arguments = ''
            x.ld_target_path = x.output_file

            if x.target_config.endswith('_test') and x.output_file.endswith('.dll'):
                x.ld_target_command = '$(OutDir)AzTestRunner'
                x.ld_target_arguments = "{} AzRunUnitTests --pause-on-completion --gtest_break_on_failure".format(x.output_file)

            elif self.engine_external and hasattr(self.tg, 'tool_launcher_target'):
                tool_launcher_target = getattr(self.tg, 'tool_launcher_target', None)
                if tool_launcher_target is not None:
                    if isinstance(tool_launcher_target, list):
                        tool_launcher_target = tool_launcher_target[0]

                    target_executable = "{}.exe".format(tool_launcher_target)
                    # Tool Launcher targets will exist in the corresponding engine's outdir folder
                    target_executable_full_path = os.path.join(self.tg.bld.engine_path, os.path.basename(x.output_path), target_executable)
                    x.ld_target_path = target_executable_full_path
                    x.ld_target_arguments = '--app-root "{}"'.format(self.tg.bld.path.abspath())
                    x.is_external_project_tool = str(True)


    def get_output_folder_node(self, waf_configuration, waf_platform):
        # Handle projects that have a custom output folder
        if waf_configuration == 'debug':
            # First check for a platform+debug specific output folder for debug configurations
            custom_output_folder_key = '{}_debug_output_folder'.format(waf_platform)
            output_folder_nodes = getattr(self.tg, custom_output_folder_key, None)
        else:
            # If the configuration is not debug, first check for the ndebug configuration markup
            custom_output_folder_key = '{}_ndebug_output_folder'.format(waf_platform)
            output_folder_nodes = getattr(self.tg, custom_output_folder_key, None)
            if output_folder_nodes is None:
                # If ndebug marker was not used, see if the actual configuration is specified
                custom_output_folder_key = '{}_{}_output_folder'.format(waf_platform, waf_configuration)
                output_folder_nodes = getattr(self.tg, custom_output_folder_key, None)
        if output_folder_nodes is None:
            # If a configuration specific output folder was not found, search just by platform
            custom_output_folder_key = '{}_output_folder'.format(waf_platform)
            output_folder_nodes = getattr(self.tg, custom_output_folder_key, None)
        if output_folder_nodes is None:
            # If no custom output folder is found, use the default
            output_folder_node = self.ctx.get_output_folders(waf_platform, waf_configuration, None, self.name)[0]
        elif len(output_folder_nodes) > 0:
            # If a custom output folder was found, convert it to a Node.
            # Note: It is assumed that the output folder is relative to the root folder
            output_folder_node = self.ctx.path.make_node(output_folder_nodes[0])
        else:
            Logs.error('Invalid output folder defined for project {}'.format(self.tg.output_file_name))
        if hasattr(self.tg, 'output_sub_folder'):
            output_folder_node = output_folder_node.make_node(self.tg.output_sub_folder)
        return output_folder_node


@conf
def get_enabled_specs(ctx):
    enabled_specs = [spec for spec in ctx.loaded_specs() if ctx.is_allowed_spec(spec)]
    return enabled_specs


class msvs_legacy(BuildContext):
    """
    Command to handle the legacy 'msvs' command to inject the current msvs_<vs version> command(s) based on
    the platform's availability
    """
    
    after = ['gen_spec_use_map_file']
    cmd = 'msvs'
    is_project_generator = True
    
    def execute(self):
        
        # Check all the msvc related platforms
        msvc_target_platforms = self.get_platform_aliases('msvc')
        
        for msvc_target_platform_name in msvc_target_platforms:
            
            
            msvc_target_platform = self.get_platform_settings(msvc_target_platform_name)
            msvs_table = msvc_target_platform.attributes.get('msvs')
            if not msvs_table:
                # Skip platforms without a 'msvs' attribute
                continue
            msvs_command = msvs_table.get('command')
            if not msvs_command:
                # Skip platforms that don't define an msvs 'command'
                continue
            try:
                # Check if the platform is enabled or not before appending the msvs command for it
                enable_setting_key = 'enable_{}'.format(msvc_target_platform_name)
                if self.is_option_true(enable_setting_key):
                    Options.commands.append(msvs_command)
            except:
                pass

class msvs_generator(BuildContext):
    """
    Generates a Visual Studio solution
    """
    after = ['gen_spec_use_map_file']

    is_project_generator = True
    cmd = 'legacy_msvs'
    fun = 'build'

    def init(self, vs_name, msvs_version, format_ver, msvs_ver, compat_toolsets, vs_solution_name, vs_installation_path):

        # Initialize the VS platform's msvs values
        self.msvs_version = msvs_version
        self.formatver = format_ver
        self.vsver = msvs_ver
        self.compat_toolsets = compat_toolsets
        self.defaultPlatformToolSet = compat_toolsets[0] if isinstance(compat_toolsets, list) else ''
        self.solution_name = vs_solution_name
        self.dep_proj_folder = '{}.depproj'.format(os.path.join(self.options.visual_studio_solution_folder,os.path.splitext(vs_solution_name)[0]))

        # Track the installation path to visual studio
        self.vs_installation_path = vs_installation_path

        # Collect the list of specs that are enabled for this solution
        self.specs_for_solution = self.get_specs_for_solution()

        # Convert -p <spec> to --specs-to-include-in-project-generation=<spec> --visual-studio-solution-name=<spec name> --enabled-game-projects=<spec game projects>
        if self.options.project_spec and self.options.project_spec != 'all':
            spec = self.options.project_spec
            solution_name = '{}_{}.sln'.format(self.spec_visual_studio_name(spec).replace(' ', ''), self.vs_name)
            self.specs_for_solution = [spec]
            self.solution_name = solution_name
            self.dep_proj_folder = '{}.depproj'.format(os.path.join(self.options.visual_studio_solution_folder, os.path.splitext(self.solution_name)[0]))

        restricted_platforms = self.get_restricted_platforms_from_spec_list(self.specs_for_solution)

        self.platform_map, self.toolset_map = self.get_compatible_platform_to_toolset_maps(msvs_ver, restricted_platforms)

        Logs.info('[INFO] Generating {} solution.'.format(vs_name))

        # default project is used to sort one project to the top of the projects list.  That will cause it to become
        # the default startup project if its not overridden in the .suo file
        self.default_project = self.options.default_project

        # Populate the 'configurations' attributes
        self.configurations = self.get_enabled_vs_configurations(platforms_list=list(self.platform_map.values()),
                                                                 enabled_specs=self.specs_for_solution)

        if not getattr(self, 'all_projects', None):
            self.all_projects = []
        if not getattr(self, 'project_extension', None):
            self.project_extension = '.vcxproj'
        if not getattr(self, 'projects_dir', None):
            self.projects_dir = self.srcnode.make_node(self.dep_proj_folder)
            self.projects_dir.mkdir()
        # bind the classes to the object, so that subclass can provide custom generators
        if not getattr(self, 'vsnode_vsdir', None):
            self.vsnode_vsdir = vsnode_vsdir
        if not getattr(self, 'vsnode_target', None):
            self.vsnode_target = vsnode_target
        if not getattr(self, 'vsnode_build_all', None):
            self.vsnode_build_all = vsnode_build_all
        if not getattr(self, 'vsnode_install_all', None):
            self.vsnode_install_all = vsnode_install_all
        if not getattr(self, 'vsnode_project_view', None):
            self.vsnode_project_view = vsnode_project_view

        self.waf_project = None

        self.vs_globals = {}
        # Retrieve vsglobal msbuild Properties per platform
        for platform in self.platform_map:
            platform_env = self.all_envs.get(platform, ConfigSet())
            if 'VS_GLOBALS' in platform_env:
                self.vs_globals.update(platform_env['VS_GLOBALS'])

        return True

    def get_compatible_platform_to_toolset_maps(self, msvs_version, restricted_platforms):
        """

        :param msvs_version:
        :param platform_toolset:
        :return:
        """
        # Go through the list of enabled platforms and track which ones 'compatible' toolsets apply to the current toolset
        compatible_platforms_map = {}
        ms_toolset_to_platform_map = {}

        enabled_platforms = self.get_all_target_platforms()
        for enabled_platform in enabled_platforms:

            # Is this an msvs compatible platform at all?
            msvs_attributes = enabled_platform.attributes.get('msvs', None)
            if not msvs_attributes:
                continue

            if restricted_platforms:
                # If there is a platform restriction, then check if the platforms name or any of its aliases
                # conform to the list of restricted platforms
                if enabled_platform.platform not in restricted_platforms and \
                        enabled_platform.aliases.intersection(restricted_platforms):
                    continue

            # Get this platform's toolset name
            platform_toolset_name = msvs_attributes.get('toolset_name', None)
            if not platform_toolset_name:
                continue

            toolset_properties_file = self.get_msbuild_toolset_properties_file_path(msvs_version, platform_toolset_name)
            if not os.path.isfile(toolset_properties_file):
                continue

            compatible_platforms_map[enabled_platform.platform] = enabled_platform
            if platform_toolset_name not in ms_toolset_to_platform_map:
                ms_toolset_to_platform_map[platform_toolset_name] = []
            ms_toolset_to_platform_map[platform_toolset_name].append(enabled_platform)

        return compatible_platforms_map, ms_toolset_to_platform_map

    def get_msbuild_toolset_properties_file_path(self, toolset_version, toolset_name):

        ms_build_root = self.get_msbuild_root(toolset_version)
        platform_root_dir = os.path.join(ms_build_root, 'Microsoft.Cpp', 'v4.0', 'V{}0'.format(toolset_version),
                                         'Platforms', toolset_name)
        platform_props_file = os.path.join(platform_root_dir, 'Platform.props')
        return platform_props_file

    def is_allowed_spec(self, spec_name):
        """
        Check if the spec should be included when generating visual studio projects
        :param spec_name:   The spec name to validate
        :return: True if its an enabled spec for this msvs process, False if not
        """
        if self.options.specs_to_include_in_project_generation == '':
            return True
        allowed_specs = [spec.strip() for spec in self.options.specs_to_include_in_project_generation.split(',')]
        if spec_name in allowed_specs:
            return True
        return False

    def get_specs_for_solution(self):
        enabled_specs = self.get_enabled_specs()
        specs_for_solution = [spec for spec in enabled_specs if self.is_allowed_spec(spec)]
        return specs_for_solution

    def get_restricted_platforms_from_spec_list(self, spec_list):
        """
        Inspect any specified spec and see if there are any platform restrictions.
        :return: Set of any platform restrictions
        """
        # Collect all the restricted platforms from the spec (if any)
        restricted_platforms_set = set()
        for spec in spec_list:
            restricted_platforms_for_spec = self.preprocess_target_platforms(self.spec_platforms(spec))
            for restricted_platform in restricted_platforms_for_spec:
                restricted_platforms_set.add(restricted_platform)
        return restricted_platforms_set

    def get_enabled_vs_platforms(self):
        """
        Build a list of supported platforms (vs platform name) to use to build the solution
        :param restricted_platforms: Optional set of platforms to restrict to
        :return: List of vs platform names
        """
        # Populate the 'platforms' attributes (vs platforms)
        vs_platforms = []
        for waf_platform, msvs_attributes in list(self.waf_platform_to_attributes.items()):
            vs_platforms.append(msvs_attributes['toolset_name'])
        return vs_platforms

    def get_enabled_vs_configurations(self, platforms_list, enabled_specs):
        """
        Base on the enabled specs, build up a list of all the configurations
        """
        # Go through each of platforms and collect the common configurations
        platform_configurations_set = set()
        for platform in platforms_list:
            platform_config_details = platform.get_configuration_details()
            for platform_config_detail in platform_config_details:
                platform_configurations_set.add((platform_config_detail.config_name(), platform_config_detail))
        platform_configurations_list = list(platform_configurations_set)
        platform_configurations_list.sort(key=lambda plat: plat[0])

        vs_configurations = set()

        # Each VS configuration is based on the spec at the top level
        for waf_spec in enabled_specs:
            vs_spec_name = self.convert_waf_spec_to_vs_spec(waf_spec)
            restricted_configurations_for_spec = self.preprocess_target_configuration_list(None, None,
                                                                                           self.spec_configurations(
                                                                                               waf_spec))
            exclude_test_configurations = self.spec_exclude_test_configurations(waf_spec)
            exclude_monolithic_configurations = self.spec_exclude_monolithic_configurations(waf_spec)

            for platform_config in platform_configurations_list:
                if restricted_configurations_for_spec and platform_config[0] not in restricted_configurations_for_spec:
                    continue
                if exclude_test_configurations and platform_config[1].is_test:
                    continue
                if exclude_monolithic_configurations and platform_config[1].settings.is_monolithic:
                    continue

                vs_config = VS_Configuration(vs_spec_name, waf_spec, platform_config[0])

                # TODO: Make sure that there is at least one module that is defined for the platform and configuration
                vs_configurations.add(vs_config)

        return list(sorted(vs_configurations))

    def execute(self):
        """
        Entry point
        """
        self.restore()
        if not self.all_envs:
            self.load_envs()

        self.env['PLATFORM'] = 'project_generator'
        self.env['CONFIGURATION'] = 'project_generator'

        self.load_user_settings()

        Logs.info("[WAF] Executing '%s' in '%s'" % (self.cmd, self.variant_dir))
        self.recurse([self.run_dir])

        # user initialization
        try:
            platform_settings = settings_manager.LUMBERYARD_SETTINGS.get_platform_settings(self.platform_name)
            msvs_settings_dict = platform_settings.attributes.get('msvs')
            format_ver = msvs_settings_dict.get('format_ver')
            msvs_ver = msvs_settings_dict.get('msvs_ver')
            compat_toolsets = msvs_settings_dict.get('compat_toolsets')
            vs_name = msvs_settings_dict.get('vs_name')
            vs_solution_name = '{}.sln'.format(getattr(self.options, self.vs_solution_name_options_attribute, 'LumberyardSDK'))

            if self.platform_name not in self.all_envs:
                raise Errors.WafError("Unable to locate environment for '{}'. Make sure to run lmbr_waf configure.".format(self.platform_name))

            platform_env = self.all_envs[self.platform_name]
            vs_installation_path = platform_env['VS_INSTALLATION_PATH']
            if not vs_installation_path:
                raise Errors.WafError(
                    "Unable to determine the visual studio installation path for '{}'. Make sure to run lmbr_waf configure.".format(
                        self.platform_name))

            self.init(vs_name=vs_name,
                      msvs_version=str(self.msvs_version),
                      format_ver=format_ver,
                      msvs_ver=msvs_ver,
                      compat_toolsets=compat_toolsets,
                      vs_solution_name=vs_solution_name,
                      vs_installation_path=vs_installation_path)

            # two phases for creating the solution
            self.collect_projects()  # add project objects into "self.all_projects"
            self.write_files()  # write the corresponding project and solution files
            self.post_build()

        except Errors.WafError as err:
            Logs.info("{}. Skipping visual studio solution generation".format(str(err)))

    def collect_projects(self):
        """
        Fill the list self.all_projects with project objects
        Fill the list of build targets
        """
        self.collect_targets()
        self.add_aliases()
        self.collect_dirs()
        default_project = getattr(self, 'default_project', None)

        def sortfun(x):
            if x.name == default_project:
                return ''
            return getattr(x, 'path', None) and x.path.abspath() or x.name

        self.all_projects.sort(key=sortfun)

    def write_files(self):
        """
        Write the project and solution files from the data collected
        so far. It is unlikely that you will want to change this
        """
        for p in self.all_projects:
            Logs.debug('lumberyard:  Writing Visual Studio project for module %s ' % p.name)
            p.write()

        # and finally write the solution file
        node = self.get_solution_node()
        node.parent.mkdir()
        Logs.warn('Creating %r' % node)
        template1 = compile_template(SOLUTION_TEMPLATE)
        sln_str = template1(self)
        sln_str = rm_blank_lines(sln_str)
        node.stealth_write(sln_str)

    def get_solution_node(self):
        """
        The solution filename is required when writing the .vcproj files
        return self.solution_node and if it does not exist, make one
        """
        try:
            return self.solution_node
        except:
            pass

        solution_name = getattr(self, 'solution_name', None)
        if not solution_name:
            solution_name = getattr(Context.g_module, Context.APPNAME, 'project') + '.sln'
        if os.path.isabs(solution_name):
            self.solution_node = self.root.make_node([self.options.visual_studio_solution_folder, solution_name])
        else:
            self.solution_node = self.srcnode.make_node([self.options.visual_studio_solution_folder, solution_name])
        return self.solution_node

    def project_configurations(self):
        """
        Helper that returns all the pairs (config,toolset_name (for the platform)
        """
        ret = []
        for c in self.configurations:
            for platform_name, platform_def in list(self.platform_map.items()):
                msvs_dict = platform_def.attributes.get('msvs', None)
                if not msvs_dict:
                    raise Errors.WafError("Platform settings for '{}' is missing the required 'msvs' attribute to configure msvs projects.".format(platform_name))
                toolset_name = msvs_dict.get('toolset_name', None)
                if not toolset_name:
                    raise Errors.WafError("Platform settings for '{}' is missing the required 'toolset_name' in its 'msvs' attribute to configure msvs projects.".format(platform_name))
                ret.append((str(c), toolset_name))
        return ret

    def collect_targets(self):
        """
        Process the list of task generators
        """

        # we will iterate over every platform and configuration permutation
        # and add any that match the current "live" set to the project.

        qualified_taskgens = []
        unqualified_taskgens = []

        # Collect all the relevant task gens into a processing list
        taskgen_list = []
        target_to_taskgen = {}
        for group in self.groups:
            for task_gen in group:
                if isinstance(task_gen, TaskGen.task_gen) and hasattr(task_gen, 'project_filter'):
                    taskgen_list.append(task_gen)
                    target_to_taskgen[task_gen.target] = task_gen

        # Prepare a whitelisted list of targets that are eligible due to use lib dependencies
        whitelisted_targets = set()
        qualified_taskgens_targetnames = set()

        # First pass is to identify the eligible modules based on the spec/platform/configuration rule
        for taskgen in taskgen_list:

            target = taskgen.target

            # Get the platform restrictions from the module definitions
            module_platforms = self.get_module_platforms(target)

            skip_module = True

            # Check against the spec/platform/configuration standard rules
            for spec_name in self.specs_for_solution:
                skip_module = not self.check_against_platform_config_rules(module_platforms, spec_name, target)
                if not skip_module:
                    break
            # Check if implied through the enabled game project
            if skip_module:
                skip_module = not self.check_against_enabled_game_projects(target)
                
            if skip_module:
                Logs.debug('msvs: Unqualifying %s' % taskgen.target)
                unqualified_taskgens.append(taskgen)
                continue

            qualified_taskgens.append(taskgen)
            qualified_taskgens_targetnames.add(target)

            module_uses = self.get_module_uses_for_taskgen(taskgen, target_to_taskgen)
            for module_use in module_uses:
                whitelisted_targets.add(module_use)

        # Second pass, go through the initial unqualified task gens and see if they qualify based on the whitelisted targets
        for taskgen in unqualified_taskgens:
            if taskgen.target in whitelisted_targets:
                qualified_taskgens.append(taskgen)
            else:
                unit_test_target = getattr(taskgen, 'unit_test_target', None)
                if unit_test_target and unit_test_target in qualified_taskgens_targetnames:
                    qualified_taskgens.append(taskgen)

        # Iterate through the qualified taskgens and create project nodes for them
        for taskgen in qualified_taskgens:

            Logs.debug('lumberyard:  Creating Visual Studio project for module %s ' % target)

            if not hasattr(taskgen, 'msvs_includes'):
                taskgen.msvs_includes = taskgen.to_list(getattr(taskgen, 'includes', [])) + taskgen.to_list(
                    getattr(taskgen, 'export_includes', []))
                taskgen.post()

            p = self.vsnode_target(self, taskgen)
            p.collect_source()  # delegate this processing
            p.collect_properties()
            p.vs_globals = self.vs_globals
            self.all_projects.append(p)

    def check_against_enabled_game_projects(self, target):

        # Check the project settings....
        for project in self.get_enabled_game_project_list():
            for platform in list(self.platform_map.values()):
                if 'msvs' not in platform.attributes:
                    assert False, 'msvs missing from {}'.format(platform.platform)
                platform_prefix = platform.attributes['msvs']['prefix']
                if target in self.project_and_platform_modules(project, platform_prefix):
                    return True

        return False

    def check_against_platform_config_rules(self, module_platforms, spec_name, target):

        for platform in list(self.platform_map.values()):

            platform_name = platform.platform

            # The module must support the platform
            if platform_name not in module_platforms:
                continue

            # Get any configuration restrictions for the current platform
            module_configurations = self.get_module_configurations(target, platform_name)

            # Go through each of the possible configurations to make sure that this module qualifies
            for vs_configuration in self.configurations:

                waf_configuration = vs_configuration.waf_configuration

                # The module must support the configuration
                if waf_configuration not in module_configurations:
                    continue

                # The module must exist in the spec
                all_spec_modules = self.spec_modules(spec_name, platform_name, waf_configuration)

                if target in all_spec_modules:
                    return True

        return False

    def get_module_uses_for_taskgen(self, taskgen, target_to_taskgen):

        def _recurse_find_module_use(taskgen, target_to_taskgen, visited):

            module_uses = []
            if taskgen.target in visited:
                return module_uses

            visited.add(taskgen.target)

            use_related_keywords = self.get_all_eligible_use_keywords()
            taskgen_uses = []
            for use_keyword in use_related_keywords:
                append_to_unique_list(taskgen_uses, getattr(taskgen, use_keyword, []))

            for taskgen_use in taskgen_uses:
                module_uses.append(taskgen_use)
                if taskgen_use in target_to_taskgen:
                    module_uses += _recurse_find_module_use(target_to_taskgen[taskgen_use], target_to_taskgen, visited)
            return module_uses

        visited = set()
        return _recurse_find_module_use(taskgen, target_to_taskgen, visited)

    def add_aliases(self):
        """
        Add a specific target that emulates the "make all" necessary for Visual studio when pressing F7
        We also add an alias for "make install" (disabled by default)
        """
        base = getattr(self, 'projects_dir', None) or self.tg.path

        node_project = base.make_node('_WAF_' + self.project_extension) # Node.
        p_build = self.vsnode_build_all(self, node_project)
        p_build.collect_properties()
        p_build.vs_globals = self.vs_globals
        self.all_projects.append(p_build)
        self.waf_project = p_build

    def collect_dirs(self):
        """
        Create the folder structure in the Visual studio project view
        """
        seen = {}

        for p in self.all_projects[:]: # iterate over a copy of all projects
            if not getattr(p, 'tg', None):
                # but only projects that have a task generator
                continue

            if getattr(p, 'parent', None):
                # aliases already have parents
                continue

            filter_name = self.get_project_vs_filter(p.name)

            proj = p
            # folder list contains folder-only names of the relative project path
            folder_list = filter_name.split('/')
            # filter list contains collection of all relative paths leading to project path
            filter_list = []
            filter = ''
            for folder in folder_list:
                if filter:
                    filter += '/'
                filter += folder
                filter_list.append(filter)
            filter_list.reverse()
            for filter in filter_list:
                if filter in seen:
                    proj.parent = seen[filter]
                    break
                else:
                    # get immediate parent folder
                    folder = filter.split('/')[-1]
                    n = proj.parent = seen[filter] = self.vsnode_vsdir(self, make_uuid(filter), folder)
                    self.all_projects.append(n)
                    proj = n


def options(ctx):
    """
    If the msvs option is used, try to detect if the build is made from visual studio
    """
    ctx.add_option('--execsolution', action='store', help='when building with visual studio, use a build state file')

    old = BuildContext.execute

    def override_build_state(ctx):
        def lock(rm, add):
            uns = ctx.options.execsolution.replace('.sln', rm)
            uns = ctx.root.make_node(uns)
            try:
                Logs.debug('msvs: Removing %s' % uns.abspath())
                uns.delete()
            except:
                pass

            uns = ctx.options.execsolution.replace('.sln', add)
            uns = ctx.root.make_node(uns)
            try:
                Logs.debug('msvs: Touching %s' % uns.abspath())
                uns.write('')
            except:
                pass

        if ctx.options.execsolution:
            ctx.launch_dir = Context.top_dir # force a build for the whole project (invalid cwd when called by visual studio)
            modtime = time.mktime(datetime.datetime.now().timetuple())
            lock('.lastbuildstate', '.unsuccessfulbuild')
            old(ctx)
            lock('.unsuccessfulbuild', '.lastbuildstate')
            lbs = ctx.options.execsolution.replace('.sln', '.lastbuildstate')
            lbs = ctx.root.make_node(lbs)
            atime = time.mktime(datetime.datetime.now().timetuple())
            os.utime(lbs.abspath(), (atime, modtime))
        else:
            old(ctx)

    BuildContext.execute = override_build_state


