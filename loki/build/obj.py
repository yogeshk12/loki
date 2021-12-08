import re
from pathlib import Path

try:
    from cached_property import cached_property
except ImportError:
    try:
        from functools import cached_property
    except ImportError:
        def cached_property(func):
            return func

from loki.logging import debug
from loki.tools import execute, as_tuple, flatten, cached_func
from loki.build.compiler import _default_compiler
from loki.build.header import Header


__all__ = ['Obj']


_re_use = re.compile(r'^\s*use\s+(?P<use>\w+)', re.IGNORECASE | re.MULTILINE)
_re_include = re.compile(r'\#include\s+["\']([\w\.]+)[\"\']', re.IGNORECASE)
# Please note that the below regexes are fairly expensive due to .* with re.DOTALL
_re_module = re.compile(r'module\s+(\w+).*?end module', re.IGNORECASE | re.DOTALL)
_re_subroutine = re.compile(r'subroutine\s+(\w+).*?end subroutine', re.IGNORECASE | re.DOTALL)


class Obj:
    """
    A single source object representing a single C or Fortran source file.
    """

    MODEMAP = {'.f90': 'f90', '.f': 'f', '.c': 'c', '.cc': 'c'}

    # Default source and header extension recognized
    # TODO: Make configurable!
    _ext = ['.f90', '.F90', '.f', '.F', '.c']

    def __new__(cls, *args, name=None, **kwargs):  # pylint: disable=unused-argument
        # Name is either provided or inferred from source_path
        name = name or Path(kwargs.get('source_path')).stem
        name = name.lower()  # Ensure no-caps!

        # Return an instance cached on the derived or provided name
        # TODO: We could make the path relative to a "cache path" here...
        return Obj.__xnew_cached_(cls, name)

    def __new_stage2_(self, name):  # pylint: disable=unused-private-member
        obj = super().__new__(self)
        obj.name = name
        return obj

    __xnew_cached_ = staticmethod(cached_func(__new_stage2_))

    def __init__(self, name=None, source_path=None):  # pylint: disable=unused-argument
        self.path = None  # The eventual .o path
        self.q_task = None  # The parallel worker task

        if not hasattr(self, 'source_path'):
            # If this is the first time, establish the source path
            self.source_path = Path(source_path or self.name)  # pylint: disable=no-member

            if not self.source_path.exists():
                debug('Could not find source file for %s', self)
                self.source_path = None

    def __repr__(self):
        return 'Obj<%s>' % self.name  # pylint: disable=no-member

    @cached_property
    def source(self):
        if self.source_path is not None:
            # TODO: Make encoding a global config item.
            with self.source_path.open(encoding='latin1') as f:
                source = f.read()
            return source
        return None

    @cached_property
    def modules(self):
        return list(_re_module.findall(self.source))

    @cached_property
    def subroutines(self):
        return list(_re_subroutine.findall(self.source))

    @cached_property
    def uses(self):
        if self.source is None:
            return []
        return list(_re_use.findall(self.source))

    @cached_property
    def includes(self):
        return list(_re_include.findall(self.source))

    @property
    def dependencies(self):
        """
        Names of build items that this item depends on.
        """
        if self.source is None:
            return ()

        # Pick out the header object from imports
        includes = [Path(incl).stem for incl in self.includes]
        includes = [Path(incl).stem if '.intfb' in incl else incl
                    for incl in includes]
        headers = [Header(name=i) for i in includes]

        # Add transitive module dependencies through header imports
        transitive = flatten(h.uses for h in headers if h.source_path is not None)
        return as_tuple(set(self.uses + transitive))

    @property
    def definitions(self):
        """
        Names of provided subroutine and modules.
        """
        return as_tuple(self.modules + self.subroutines)

    def build(self, builder=None, logger=None, compiler=None,
              workqueue=None, force=False, include_dirs=None):
        """
        Execute the respective build command according to the given
        :param toochain:.

        Please note that this does not build any dependencies.
        """
        logger = logger or builder.logger
        compiler = compiler or builder.compiler
        build_dir = builder.build_dir
        include_dirs = (include_dirs or []) + ((builder.include_dirs if builder else None) or [])
        include_dirs = include_dirs if len(include_dirs) > 0 else None

        if self.source_path is None:
            raise RuntimeError('No source file found for %s' % self)

        mode = self.MODEMAP[self.source_path.suffix.lower()]
        source = self.source_path.absolute()
        target = (build_dir/self.name).with_suffix('.o')  # pylint: disable=no-member
        t_time = target.stat().st_mtime if target.exists() else None
        s_time = source.stat().st_mtime if source.exists() else None

        if not force and t_time is not None and s_time is not None \
           and t_time > s_time:
            logger.debug('%s up-to-date, skipping...' % self)
            return

        args = compiler.compile_args(source=source, include_dirs=include_dirs,
                                     target=target, mode=mode, mod_dir=build_dir)

        if workqueue is not None:
            self.q_task = workqueue.execute(args, log_queue=workqueue.log_queue)
        else:
            execute(args)

    def wrap(self, builder=None):
        """
        Wrap the compiled object using ``f90wrap`` and return the loaded module.
        """
        build_dir = str(builder.build_dir)
        compiler = builder.compiler or _default_compiler

        module = self.source_path.stem
        source = [str(self.source_path)]
        compiler.f90wrap(modname=module, source=source, cwd=build_dir)

        # Execute the second-level wrapper (f2py-f90wrap)
        wrapper = 'f90wrap_%s.f90' % self.source_path.stem
        if self.modules is None or len(self.modules) == 0:
            wrapper = 'f90wrap_toplevel.f90'
        compiler.f2py(modname=module, source=[wrapper, '%s.o' % self.source_path.stem],
                      cwd=build_dir)

        return builder.load_module(module)
