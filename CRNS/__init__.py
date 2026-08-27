compiling = False
noticed = False
notice_delay = 0.3
import os
import sys
import time
import threading
from importlib.util import find_spec
if find_spec("pyximport") and find_spec("cython"):
    import json
    from importlib.abc import MetaPathFinder
    from importlib.machinery import ExtensionFileLoader, SourceFileLoader
    from importlib.machinery import EXTENSION_SUFFIXES
    from importlib.util import spec_from_file_location
    from distutils.extension import Extension

    from pyximport import pyxbuild

    class _RNSImportFinder(MetaPathFinder):
        """A cythonizing import hook for the RNS package tree.

        pyximport's own ``.py`` hook is unsuitable for this: it only
        resolves top-level module names (so every sub-module import
        misses the file), and it disables itself after compiling a
        single module.

        This finder compiles RNS source modules into a mirror tree
        under ``~/.pyxbld/rnslib/`` that exactly mirrors the source
        package layout, so that:

          * ``os.path.dirname(__file__)`` of a compiled package
            initialiser equals that package's own mirror directory,
            keeping the ``__all__`` globs in package ``__init__``
            modules correct when the modules are compiled.
          * sub-module resolution through ``__path__`` finds the
            compiled extensions in the mirror.
          * each loaded module is a C extension (``.so``/``.pyd``).

        The entire RNS source tree is compiled up front (on first use,
        and incrementally on later uses), so the mirror is complete
        before any RNS module is imported and imported modules can be
        loaded straight from it.

        Third-party and standard-library modules are left untouched,
        and the ``RNS.vendor`` subtree is deliberately imported from
        source. Modules that cannot be compiled fall back to the
        interpreted source module with a warning.
        """

        def __init__(self):
            self.build_dir = os.path.join(os.path.expanduser("~"), ".pyxbld")
            self.mirror_root = os.path.join(self.build_dir, "rnslib")
            self.source_root = self._find_source_root()
            self.abi_suffix = EXTENSION_SUFFIXES[0]
            self.unbuildable = {}
            self.unbuildable_path = os.path.join(self.build_dir, "unbuildable.json")
            try:
                with open(self.unbuildable_path, "r") as fh:
                    self.unbuildable = json.load(fh)
            except Exception:
                self.unbuildable = {}

        def _save_unbuildable(self):
            try:
                os.makedirs(os.path.dirname(self.unbuildable_path), exist_ok=True)
                with open(self.unbuildable_path, "w") as fh:
                    json.dump(self.unbuildable, fh, indent=1)
            except Exception:
                pass

        def _mark_unbuildable(self, fullname, source):
            try:
                self.unbuildable[fullname] = int(os.path.getmtime(source))
                self._save_unbuildable()
            except Exception:
                pass

        def _unmark_buildable(self, fullname):
            if fullname in self.unbuildable:
                del self.unbuildable[fullname]
                self._save_unbuildable()

        def _is_unbuildable(self, fullname, source):
            if fullname not in self.unbuildable:
                return False
            try:
                if int(os.path.getmtime(source)) == self.unbuildable[fullname]:
                    return True
            except Exception:
                pass
            self.unbuildable.pop(fullname, None)
            return False

        def _find_source_root(self):
            here = os.path.dirname(os.path.abspath(__file__))
            candidate = os.path.abspath(os.path.join(here, "..", "RNS", "__init__.py"))
            if os.path.exists(candidate):
                return os.path.dirname(os.path.dirname(candidate))
            for entry in [os.getcwd()] + list(sys.path):
                if not entry:
                    continue
                candidate = os.path.join(entry, "RNS", "__init__.py")
                if os.path.exists(candidate):
                    return os.path.dirname(candidate)
            raise RuntimeError("CRNS could not locate the RNS source tree")

        def _all_source_modules(self):
            rns_root = os.path.join(self.source_root, "RNS")
            for root, dirs, files in os.walk(rns_root):
                dirs[:] = [d for d in dirs if d not in ("__pycache__", "vendor")]
                rel = os.path.relpath(root, self.source_root).replace(os.sep, ".")
                for fn in sorted(files):
                    if not fn.endswith(".py"):
                        continue
                    if fn == "__init__.py":
                        fullname = rel
                    else:
                        fullname = rel + "." + fn[:-3]
                    yield fullname

        def _mirror_target(self, fullname, is_package):
            rel = fullname.split(".")
            basename = rel[-1]
            if is_package:
                return os.path.join(self.mirror_root, *rel, "__init__" + self.abi_suffix)
            return os.path.join(self.mirror_root, *rel[:-1], basename + self.abi_suffix)

        def _source(self, fullname):
            rel = fullname.split(".")
            package_source = os.path.join(self.source_root, *rel, "__init__.py")
            if os.path.exists(package_source):
                return package_source, True
            module_source = os.path.join(self.source_root, *rel) + ".py"
            if os.path.exists(module_source):
                return module_source, False
            return None, None

        def _compile(self, fullname, source):
            basename = fullname.rsplit(".", 1)[-1]
            extension = Extension(name=basename, sources=[source])
            so_path = pyxbuild.pyx_to_dll(
                source,
                ext=extension,
                build_in_temp=True,
                pyxbuild_dir=self.build_dir,
            )
            abi_suffix = os.path.basename(so_path)[len(basename):]
            return so_path, abi_suffix

        def _ensure_compiled(self, fullname, source, is_package):
            target = self._mirror_target(fullname, is_package)
            if os.path.exists(target):
                return target
            so_path, abi_suffix = self._compile(fullname, source)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            os.replace(so_path, target)
            return target

        def precompile(self):
            if not os.path.isdir(self.mirror_root):
                os.makedirs(self.mirror_root, exist_ok=True)
            for fullname in self._all_source_modules():
                source, is_package = self._source(fullname)
                if source is None:
                    continue
                if os.path.exists(self._mirror_target(fullname, is_package)):
                    self._unmark_buildable(fullname)
                    continue
                if self._is_unbuildable(fullname, source):
                    continue
                try:
                    self._ensure_compiled(fullname, source, is_package)
                    self._unmark_buildable(fullname)
                except Exception as e:
                    self._mark_unbuildable(fullname, source)
                    print(f"CRNS: could not compile {fullname}, "
                          f"using interpreted module: {e}", file=sys.stderr)

        def _spec(self, fullname, source, is_package, compile_attempted):
            if fullname == "RNS.vendor" or fullname.startswith("RNS.vendor."):
                rel = fullname.split(".")
                locations = [os.path.join(self.source_root, *rel)] if is_package else None
                return spec_from_file_location(
                    fullname, source,
                    loader=SourceFileLoader(fullname, source),
                    submodule_search_locations=locations,
                )

            if compile_attempted:
                try:
                    target = self._ensure_compiled(fullname, source, is_package)
                    self._unmark_buildable(fullname)
                    if is_package:
                        locations = [os.path.dirname(target)]
                    else:
                        locations = None
                    return spec_from_file_location(
                        fullname, target,
                        loader=ExtensionFileLoader(fullname, target),
                        submodule_search_locations=locations,
                    )
                except Exception as e:
                    self._mark_unbuildable(fullname, source)
                    print(f"CRNS: could not compile {fullname}, "
                          f"using interpreted module: {e}", file=sys.stderr)

            rel = fullname.split(".")
            locations = [os.path.join(self.source_root, *rel)] if is_package else None
            return spec_from_file_location(
                fullname, source,
                loader=SourceFileLoader(fullname, source),
                submodule_search_locations=locations,
            )

        def find_spec(self, fullname, path, target=None):
            if not isinstance(fullname, str):
                return None
            if not (fullname == "RNS" or fullname.startswith("RNS.")):
                return None

            source, is_package = self._source(fullname)
            if source is None:
                return None

            return self._spec(fullname, source, is_package,
                              compile_attempted=not self._is_unbuildable(fullname, source))

    _RNSFinder = _RNSImportFinder()
    sys.meta_path.insert(0, _RNSFinder)

def notice_job():
    global noticed
    started = time.time()
    while compiling:
        if time.time() > started+notice_delay and compiling:
            noticed = True
            print("Compiling RNS object code... ", end="")
            sys.stdout.flush()
            break
        time.sleep(0.1)


compiling = True
threading.Thread(target=notice_job, daemon=True).start()
_RNSFinder.precompile()
import RNS; compiling = False
if noticed: print("Done."); sys.stdout.flush()
