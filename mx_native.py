# ----------------------------------------------------------------------------------------------------
#
# Copyright (c) 2018, 2019, Oracle and/or its affiliates. All rights reserved.
# DO NOT ALTER OR REMOVE COPYRIGHT NOTICES OR THIS FILE HEADER.
#
# This code is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License version 2 only, as
# published by the Free Software Foundation.
#
# This code is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License
# version 2 for more details (a copy is included in the LICENSE file that
# accompanied this code).
#
# You should have received a copy of the GNU General Public License version
# 2 along with this work; if not, write to the Free Software Foundation,
# Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Please contact Oracle, 500 Oracle Parkway, Redwood Shores, CA 94065 USA
# or visit www.oracle.com if you need additional information or have any
# questions.
#
# ----------------------------------------------------------------------------------------------------
import abc
import collections
import errno
import filecmp
import itertools
import os
import subprocess
import sys

import mx
import mx_compdb
import mx_subst

_target_jdk = None
"""JDK for which native projects should be built."""


def _get_target_jdk():
    global _target_jdk
    if not _target_jdk:
        _target_jdk = mx.get_jdk(tag=mx.DEFAULT_JDK_TAG)
    return _target_jdk


# Support for conditional compilation based on the JDK version.
mx_subst.results_substitutions.register_no_arg('jdk_ver', lambda: str(_get_target_jdk().javaCompliance.value))


class lazy_default(object):  # pylint: disable=invalid-name
    def __init__(self, init):
        self.init = init

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return vars(instance).setdefault(self.init.__name__, self.init(instance))


class lazy_class_default(object):  # pylint: disable=invalid-name
    def __init__(self, init):
        self.init = init

    def __get__(self, instance, owner):
        try:
            return vars(self)[self.init.__name__]
        except KeyError:
            return vars(self).setdefault(self.init.__name__, self.init(owner))


class _Toolchain(object):
    def __init__(self, target_arch):
        self.target_arch = target_arch

    @property
    def target(self):
        return f'{mx.get_os()}-{self.target_arch}'

    @property
    def is_native(self):
        return self.target_arch == mx.get_arch()

    @property
    def is_available(self):
        return self.is_native

    _registry = {}

    @classmethod
    def for_(cls, target_arch):
        return cls._registry.setdefault(target_arch, _Toolchain(target_arch))


class Ninja(object):
    """Encapsulates access to Ninja (ninja).

    Abstracts the operations of the Ninja build system that are necessary for
    the NinjaBuildTask to build a NinjaProject.
    """
    binary = 'ninja'
    default_manifest = 'build.ninja'

    def __init__(self, build_dir, parallelism, targets=None):
        self.build_dir = build_dir
        self.parallelism = str(parallelism)
        self.targets = targets or []

    def needs_build(self):
        out = mx.LinesOutputCapture()
        details = mx.LinesOutputCapture()

        self._run('-n', '-d', 'explain', *self.targets, out=out, err=details)
        if details.lines:
            return True, [l for l in details.lines if l.startswith('ninja explain:')][0]
        else:
            assert out.lines == ['ninja: no work to do.']
            return False, out.lines[0]

    def compdb(self, out):
        self._run('-t', 'compdb', *self.targets, out=out)

    def build(self):
        self._run(*self.targets)

    def clean(self):
        self._run('-t', 'clean', *self.targets)

    def _run(self, *args, **kwargs):
        cmd = [self.binary, '-j', self.parallelism]
        mx_verbose_env = mx.get_env('MX_VERBOSE', None)
        if mx.get_opts().very_verbose or mx_verbose_env:
            cmd += ['-v']
        cmd += args

        out = kwargs.get('out', mx.OutputCapture())
        err = kwargs.get('err', subprocess.STDOUT)
        verbose = mx.get_opts().verbose or mx_verbose_env
        if verbose:
            if callable(out) and '-n' not in args:
                out = mx.TeeOutputCapture(out)
            if callable(err):
                err = mx.TeeOutputCapture(err)

        rc = mx.run(cmd, nonZeroIsFatal=False, out=out, err=err, cwd=self.build_dir)
        if rc:
            mx.abort(rc if verbose else (out, err))


class NativeDependency(mx.Dependency):
    """A Dependency that can be included and linked in when building native projects.

    Attributes
        include_dirs : iterable of str
            Directories with headers provided by this dependency.
        libs : iterable of str
            Libraries provided by this dependency.
    """
    include_dirs = ()
    libs = ()


class MultiarchProject(mx.AbstractNativeProject, NativeDependency):
    """A Project containing native code that can be built for multiple target architectures.

    Attributes
        multiarch : list of str, optional
            Target architectures for which this project can be built (must include
            the host architecture).

            If present, the archivable results for each target architecture are in
            a separate subdir of the archive. Otherwise, the archivable results for
            the host architecture are at the root of the archive.
    """

    def __init__(self, suite, name, subDir, srcDirs, deps, workingSets, d, **kwargs):
        context = 'project ' + name
        if 'multiarch' in kwargs:
            multiarch = mx.Suite._pop_list(kwargs, 'multiarch', context)
            self.multiarch = list(set(mx_subst.results_substitutions.substitute(arch) for arch in multiarch))
            if mx.get_arch() not in self.multiarch:
                mx.abort(f'"multiarch" must contain the host architecture "{mx.get_arch()}"', context)
        else:
            self.multiarch = []
        super(MultiarchProject, self).__init__(suite, name, subDir, srcDirs, deps, workingSets, d, **kwargs)
        self.out_dir = self.get_output_root()

    @property
    def _use_multiarch(self):
        return self.multiarch and mx.get_opts().multiarch

    def getBuildTask(self, args):
        if self._use_multiarch:
            class MultiarchBuildTask(mx.Buildable, mx.TaskSequence):
                subtasks = [self._build_task(target_arch, args) for target_arch in self.multiarch]

                def execute(self):
                    super(MultiarchBuildTask, self).execute()
                    self.built = any(t.built for t in self.subtasks)

                def newestOutput(self):
                    return mx.TimeStampFile.newest(t.newestOutput() for t in self.subtasks)

            return MultiarchBuildTask(self, args)
        else:
            return self._build_task(mx.get_arch(), args)

    @abc.abstractmethod
    def _build_task(self, target_arch, args):
        """:rtype: TargetArchBuildTask"""

    def getArchivableResults(self, use_relpath=True, single=False):
        for target_arch in self.multiarch if self._use_multiarch else [mx.get_arch()]:
            toolchain = _Toolchain.for_(target_arch)
            target_arch_path = toolchain.target if self.multiarch else ''
            if toolchain.is_native or not single:
                for file_path, archive_path in self._archivable_results(target_arch, use_relpath, single):
                    yield file_path, mx.join(target_arch_path, archive_path)

    @abc.abstractmethod
    def _archivable_results(self, target_arch, use_relpath, single):
        """:rtype: typing.Iterable[(str, str)]"""


class TargetArchBuildTask(mx.AbstractNativeBuildTask):
    def __init__(self, args, project, target_arch):
        self.target_arch = target_arch
        super(TargetArchBuildTask, self).__init__(args, project)
        self.out_dir = mx.join(self.subject.out_dir, self.target_arch)

    @property
    def name(self):
        return f'{super(TargetArchBuildTask, self).name}_{self.target_arch}'

    def buildForbidden(self):
        forbidden = super(TargetArchBuildTask, self).buildForbidden()
        if not forbidden and not _Toolchain.for_(self.target_arch).is_available:
            self.subject.abort(f'Missing toolchain for {self.target_arch}.')
        return forbidden


class NinjaProject(MultiarchProject):
    """A MultiarchProject that is built using the Ninja build system.

    What distinguishes Ninja from other build systems is that its input files are
    not meant to be written by hand. Instead, they should be generated, which in
    this case is the responsibility of the NinjaProject subclasses.

    Subclasses are expected to generate an appropriate build manifest using the
    NinjaManifestGenerator.

    Attributes
        cflags : list of str, optional
            Flags used during compilation step.
        ldflags : list of str, optional
            Flags used during linking step.
        ldlibs : list of str, optional
            Flags used during linking step.
        use_jdk_headers : bool, optional
            Whether to add directories with JDK headers to the list of directories
            searched for header files. Default is False.
    """

    def __init__(self, suite, name, subDir, srcDirs, deps, workingSets, d, **kwargs):
        context = 'project ' + name
        self._cflags = mx.Suite._pop_list(kwargs, 'cflags', context)
        self._ldflags = mx.Suite._pop_list(kwargs, 'ldflags', context)
        self._ldlibs = mx.Suite._pop_list(kwargs, 'ldlibs', context)
        self.use_jdk_headers = kwargs.pop('use_jdk_headers', False)
        super(NinjaProject, self).__init__(suite, name, subDir, srcDirs, deps, workingSets, d, **kwargs)

    def isJDKDependent(self):
        """Returns whether this NinjaProject is JDK dependent.

        A NinjaProject is considered to be JDK dependent if it uses JDK headers
        or `<jdk_ver>` substitution in its `cflags` (presumably for conditional
        compilation).
        """
        return self.use_jdk_headers or any('<jdk_ver>' in f for f in self._cflags)

    def resolveDeps(self):
        super(NinjaProject, self).resolveDeps()
        self.buildDependencies += self._ninja_deps
        if self.use_jdk_headers or self.suite.getMxCompatibility().is_using_jdk_headers_implicitly(self):
            self.buildDependencies += [self._jdk_dep]

    @lazy_class_default
    def _ninja_deps(cls):  # pylint: disable=no-self-argument
        deps = []

        try:
            subprocess.check_output(['ninja', '--version'], stderr=subprocess.STDOUT)
        except OSError:
            dep = mx.library('NINJA', False)
            if dep:
                deps.append(dep)
                Ninja.binary = mx.join(dep.get_path(False), 'ninja')
            else:
                # necessary until GR-13214 is resolved
                mx.warn('Make `ninja` binary available via PATH to build native projects.')

        try:
            import ninja_syntax  # pylint: disable=unused-variable, unused-import
        except ImportError:
            dep = mx.library('NINJA_SYNTAX')
            deps.append(dep)
            module_path = mx.join(dep.get_path(False), f'ninja_syntax-{dep.version}')
            mx.ensure_dir_exists(module_path)  # otherwise, import machinery will ignore it
            sys.path.append(module_path)

        return deps

    @lazy_class_default
    def _jdk_dep(cls):  # pylint: disable=no-self-argument
        class JavaHome(NativeDependency):
            def __init__(self):
                super(JavaHome, self).__init__(mx.suite('mx'), 'JAVA_HOME', None)
                self.include_dirs = None

            def getBuildTask(self, args):
                # Ensure that the name is set correctly now that JAVA_HOME is definitely configured
                if not self.include_dirs:
                    jdk = _get_target_jdk()
                    self.name = 'JAVA_HOME=' + jdk.home
                    self.include_dirs = jdk.include_dirs
                return mx.NoOpTask(self, args)

            def _walk_deps_visit_edges(self, *args, **kwargs):
                pass

        return JavaHome()

    def _build_task(self, target_arch, args):
        return NinjaBuildTask(args, self, target_arch)

    @abc.abstractmethod
    def generate_manifest(self, output_dir, filename):
        """Generates a Ninja manifest used to build this project."""

    @property
    def cflags(self):
        return self._cflags

    @property
    def ldflags(self):
        return self._ldflags

    @property
    def ldlibs(self):
        return self._ldlibs

    @property
    def source_tree(self):
        return self._source['tree']

    @lazy_default
    def _source(self):
        source_tree = []
        source_files = collections.defaultdict(list)

        for source_dir in self.source_dirs():
            for root, _, files in os.walk(source_dir):
                rel_root = os.path.relpath(root, self.dir)
                source_tree.append(rel_root)

                # group files by extension
                grouping = collections.defaultdict(list)
                for f in files:
                    grouping[os.path.splitext(f)[1]].append(mx.join(rel_root, f))
                for ext in grouping.keys():
                    source_files[ext] += grouping[ext]

        return dict(tree=source_tree, files=source_files)


class NinjaBuildTask(TargetArchBuildTask):
    default_parallelism = 1
    """
    By default, we disable parallelism per project for the following reasons:
        #. It allows mx to build whole projects in parallel, which works well for
           smallish projects like ours.
        #. It is a safe default in terms of compatibility. Although projects may
           explicitly request greater parallelism, that may not work out of the
           box. In particular, the parallelization of debug builds on Windows may
           require special consideration.
    """

    def __init__(self, args, project, target_arch=mx.get_arch(), ninja_targets=None):
        super(NinjaBuildTask, self).__init__(args, project, target_arch)
        self._reason = None
        self._manifest = mx.join(self.out_dir, Ninja.default_manifest)
        self.ninja = Ninja(self.out_dir, self.parallelism, targets=ninja_targets)

    def __str__(self):
        return f'Building {self.name} with Ninja'

    def needsBuild(self, newestInput):
        is_needed, self._reason = super(NinjaBuildTask, self).needsBuild(newestInput)
        if is_needed:
            return True, self._reason

        if not mx.exists(self._manifest):
            self._reason = 'no build manifest'
            return True, self._reason

        mx.logv(f'Checking whether to build {self.name} with Ninja...')
        is_needed, self._reason = self.ninja.needs_build()
        return is_needed, self._reason

    def newestOutput(self):
        return mx.TimeStampFile.newest([mx.join(self.out_dir, self.subject._target)])

    def build(self):
        if not mx.exists(self._manifest) \
                or self._reason is None \
                or mx.basename(self._manifest) in self._reason \
                or 'phony' in self._reason:
            with mx.SafeFileCreation(self._manifest) as sfc:
                output_dir = os.path.dirname(sfc.tmpPath)
                tmpfilename = os.path.basename(sfc.tmpPath)
                self.subject.generate_manifest(output_dir, tmpfilename)

                if mx.exists(self._manifest) \
                        and not filecmp.cmp(self._manifest, sfc.tmpPath, shallow=False):
                    self.ninja.clean()

        with mx_compdb.CompdbCapture(self.subject.suite) as out:
            if out:
                self.ninja.compdb(out=out)
        self.ninja.build()

    def clean(self, forBuild=False):
        if not forBuild:
            try:
                mx.rmtree(self.out_dir)
            except OSError as e:
                if e.errno != errno.ENOENT:
                    raise


class NinjaManifestGenerator(object):
    """Abstracts the writing of the Ninja build manifest.

    Essentially, this is a wrapper around the `ninja_syntax.Writer` with several
    methods added to make it easier to write a NinjaProject build manifest.

    For more details about Ninja, see https://ninja-build.org/manual.html.
    """

    def __init__(self, project, output_dir, filename):
        import ninja_syntax
        self.project = project
        self.output_dir = output_dir
        self.n = ninja_syntax.Writer(open(os.path.join(output_dir, filename), 'w'))  # pylint: disable=invalid-name
        self._generate()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def newline(self):
        self.n.newline()

    def comment(self, text):
        self.n.comment(text)

    def variables(self, **kwargs):
        for key, value in kwargs.items():
            self.n.variable(key, value)
        self.newline()

    def include_dirs(self, dirs):
        def quote(path):
            has_spaces = ' ' in path or ('$project' in path and ' ' in self.project.dir)
            return f'"{path}"' if has_spaces else path

        self.variables(includes=['-I' + quote(self._resolve(d)) for d in dirs])

    def include(self, path):
        import ninja_syntax
        self.n.include(ninja_syntax.escape_path(path))

    def cc(self, source_file):  # pylint: disable=invalid-name
        return self.n.build(self._output(source_file), 'cc', self._resolve(source_file))[0]

    def cxx(self, source_file):
        return self.n.build(self._output(source_file), 'cxx', self._resolve(source_file))[0]

    def asm(self, source_file):
        asm_source = self._resolve(source_file)
        if getattr(self.project.toolchain, 'asm_requires_cpp', False):
            asm_source = self.n.build(self._output(source_file, '.asm'), 'cpp', asm_source)
        return self.n.build(self._output(source_file), 'asm', asm_source)[0]

    def ar(self, archive, members):  # pylint: disable=invalid-name
        return self.n.build(archive, 'ar', members)[0]

    def link(self, program, files):
        return self.n.build(program, 'link', files)[0]

    def linkxx(self, program, files):
        return self.n.build(program, 'linkxx', files)[0]

    def close(self):
        self.n.close()

    @staticmethod
    def _output(source_file, ext=None):
        if ext is None:
            ext = '.obj' if mx.is_windows() else '.o'
        return os.path.splitext(source_file)[0] + ext

    @staticmethod
    def _resolve(path):
        return mx.join('$project', path)

    def _generate(self):
        self.comment('Generated by mx. Do not edit.')
        self.newline()

        self.variables(ninja_required_version='1.3')

        self.comment('Directories')
        # must be relativ, otherwise doesn't compose with -fdebug-prefix-map=
        self.variables(project=os.path.relpath(self.project.dir, start=self.output_dir))

        self._generate_mx_interface()

    def _generate_mx_interface(self):
        def phony(target):
            return self.n.build(self._resolve(target), 'phony')[0]

        self.comment('Manifest dependencies')
        deps = [phony(d) for d in self.project.source_tree]
        deps += [self.project.suite.suite_py()]
        self.newline()

        self.comment('Used by mx to check...')
        self.n.rule('dry_run',
                    command='DRY_RUN $out',
                    generator=True)
        self.newline()

        self.comment('...whether manifest needs to be regenerated')
        self.n.build(Ninja.default_manifest, 'dry_run', implicit=deps)
        self.newline()


class DefaultNativeProject(NinjaProject):
    """A NinjaProject that makes many assumptions when generating a build manifest.

    It is assumed that:
        #. Directory layout is fixed:
            - `include` is a flat subdir containing public headers, and
            - `src` subdir contains sources and private headers.

        #. There is only one deliverable:
            - Kind is the value of the `native` attribute, and
            - Name is the value of the `deliverable` attribute if it is specified,
              otherwise it is derived from the `name` of the project.

        #. All source files are supported and necessary to build the deliverable.

        #. All `include_dirs` and `libs` provided by build dependencies are necessary
           to build the deliverable.

        #. The deliverable and the public headers are intended for distribution.

    Attributes
        native : {'static_lib', 'shared_lib'}
            Kind of the deliverable.

            Depending on the value, the necessary flags will be prepended to `cflags`
            and `ldflags` automatically.
        deliverable : str, optional
            Name of the deliverable. By default, it is derived from the `name` of the
            project.
    """
    include = 'include'
    src = 'src'

    _kinds = dict(
        static_lib=dict(
            target=lambda name: mx.add_lib_prefix(name) + ('.lib' if mx.is_windows() else '.a'),
        ),
        shared_lib=dict(
            target=lambda name: mx.add_lib_suffix(mx.add_lib_prefix(name)),
        ),
        executable=dict(
            target=mx.exe_suffix,
        ),
    )

    def __init__(self, suite, name, subDir, srcDirs, deps, workingSets, d, kind, **kwargs):
        self.deliverable = kwargs.pop('deliverable', name.split('.')[-1])
        self.toolchain = kwargs.pop('toolchain', 'mx:DEFAULT_NINJA_TOOLCHAIN')
        if srcDirs:
            mx.abort('"sourceDirs" is not supported for default native projects')
        srcDirs += [self.include, self.src]
        super(DefaultNativeProject, self).__init__(suite, name, subDir, srcDirs, deps, workingSets, d, **kwargs)
        try:
            self._kind = self._kinds[kind]
        except KeyError:
            mx.abort(f'"native" should be one of {list(self._kinds.keys())}, but "{kind}" is given')

        include_dir = mx.join(self.dir, self.include)
        if next(os.walk(include_dir))[1]:
            mx.abort('include directory must have a flat structure')

        self.include_dirs = [include_dir]
        if kind == 'static_lib':
            self.libs = [mx.join(self.out_dir, mx.get_arch(), self._target)]
        self.buildDependencies.append(self.toolchain)

    def resolveDeps(self):
        super(DefaultNativeProject, self).resolveDeps()
        self.toolchain = mx.distribution(self.toolchain, context=self)
        if not isinstance(self.toolchain, mx.AbstractDistribution) or not self.toolchain.get_output():
            mx.abort(f"Cannot generate manifest: the specified toolchain ({self.toolchain}) must be an AbstractDistribution that returns a value for get_output", context=self)

    @property
    def _target(self):
        return self._kind['target'](self.deliverable)

    @property
    def cflags(self):
        default_cflags = []
        if self._kind == self._kinds['shared_lib']:
            default_cflags += dict(
                windows=['-MD'],
            ).get(mx.get_os(), ['-fPIC'])

        if mx.is_morello() and self.name != "com.oracle.jvmtiasmagent":
            default_cflags += ['-O0','-Wshorten-cap-to-int','-Wcheri','-march=morello','-mabi=purecap'
                                ,'-Xclang','-morello-vararg=new','-mcpu=rainier']
            
        if mx.is_linux() or mx.is_darwin():
            # Do not leak host paths via dwarf debuginfo
            def add_debug_prefix(prefix_dir):
                def quote(path):
                    return f'"{path}"' if ' ' in path else path

                return f'-fdebug-prefix-map={quote(prefix_dir)}={quote(mx.basename(prefix_dir))}'

            default_cflags += [add_debug_prefix(self.suite.vc_dir)]
            default_cflags += [add_debug_prefix(_get_target_jdk().home)]
            default_cflags += ['-gno-record-gcc-switches']

        return default_cflags + super(DefaultNativeProject, self).cflags

    @property
    def ldflags(self):
        default_ldflags = []
        if self._kind == self._kinds['shared_lib']:
            default_ldflags += dict(
                darwin=['-dynamiclib', '-undefined', 'dynamic_lookup'],
                windows=['-dll'],
            ).get(mx.get_os(), ['-shared', '-fPIC'])
        if mx.is_morello() and self.name != "com.oracle.jvmtiasmagent":
            default_ldflags += ['-fsanitize=cheri','-Wshorten-cap-to-int','-mabi=purecap','-fuse-ld=lld']

        return default_ldflags + super(DefaultNativeProject, self).ldflags

    @property
    def h_files(self):
        return self._source['files'].get('.h', [])

    @property
    def c_files(self):
        return self._source['files'].get('.c', [])

    @property
    def cxx_files(self):
        return self._source['files'].get('.cc', [])

    @property
    def asm_sources(self):
        return self._source['files'].get('.S', [])

    def generate_manifest(self, output_dir, filename):
        unsupported_source_files = list(set(self._source['files'].keys()) - {'.h', '.c', '.cc', '.S', '.swp'})
        if unsupported_source_files:
            mx.abort(f'{unsupported_source_files} source files are not supported by default native projects')

        with NinjaManifestGenerator(self, output_dir, filename) as gen:
            gen.comment("Toolchain configuration")
            gen.include(mx.join(self.toolchain.get_output(), 'toolchain.ninja'))
            gen.newline()
            gen.variables(cflags=[mx_subst.path_substitutions.substitute(cflag) for cflag in self.cflags])
            if self._kind != self._kinds['static_lib']:
                gen.variables(
                    ldflags=[mx_subst.path_substitutions.substitute(ldflag) for ldflag in self.ldflags],
                    ldlibs=self.ldlibs,
                )
            gen.include_dirs(collections.OrderedDict.fromkeys(
                # remove the duplicates while maintaining the ordering
                [mx.dirname(h_file) for h_file in self.h_files] + list(itertools.chain.from_iterable(
                    getattr(d, 'include_dirs', []) for d in self.buildDependencies))
            ).keys())

            gen.comment('Compiled project sources')
            object_files = [gen.cc(f) for f in self.c_files]
            gen.newline()
            object_files += [gen.cxx(f) for f in self.cxx_files]
            gen.newline()
            object_files += [gen.asm(f) for f in self.asm_sources]
            gen.newline()

            gen.comment('Project deliverable')
            if self._kind == self._kinds['static_lib']:
                gen.ar(self._target, object_files)
            else:
                link = gen.linkxx if self.cxx_files else gen.link
                dep_libs = list(itertools.chain.from_iterable(getattr(d, 'libs', []) for d in self.buildDependencies))
                link(self._target, object_files + dep_libs)

    def _archivable_results(self, target_arch, use_relpath, single):
        def result(base_dir, file_path):
            assert not mx.isabs(file_path)
            archive_path = file_path if use_relpath else mx.basename(file_path)
            return mx.join(base_dir, file_path), archive_path

        yield result(mx.join(self.out_dir, target_arch), self._target)

        if not single:
            for header in os.listdir(mx.join(self.dir, self.include)):
                yield result(self.dir, mx.join(self.include, header))
