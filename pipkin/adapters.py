import os.path
import re
from abc import ABC, abstractmethod
from logging import getLogger
from typing import Optional, List, Dict, Tuple

from pipkin.common import UserError
from pipkin.util import parse_meta_dir_name

META_ENCODING = "utf-8"

logger = getLogger(__name__)


class Adapter(ABC):
    @abstractmethod
    def get_user_packages_path(self) -> Optional[str]:
        """Unix / Windows ports return the location of user packages"""
        ...

    @abstractmethod
    def get_default_target(self) -> str:
        """Installation location if neither --user nor --target is specified"""
        ...

    @abstractmethod
    def list_dists(self, paths: List[str] = None) -> Dict[str, Tuple[str, str]]:
        """Return canonic names of the distributions mapped to their meta dir names and
        installation paths.

        If a distribution is installed to different sys.path locations, then return only the first one.
        """
        ...

    @abstractmethod
    def remove_dist(
        self, dist_name: str, target: Optional[str] = None, above_target: bool = False
    ) -> None:
        """If target is given, then remove from this directory.
        If above_path, then also remove from sys.path dirs which would hide the package at path.
        Otherwise remove the first visible instance of the dist according to sys.path.
        """
        ...

    @abstractmethod
    def read_file(self, path: str) -> bytes:
        """Path must be device's absolute path (ie. start with /)"""
        ...

    @abstractmethod
    def write_file(self, path: str, content: bytes) -> None:
        """Path must be device's absolute path (ie. start with /)"""
        ...

    @abstractmethod
    def join_path(self, *parts: str) -> str:
        ...

    @abstractmethod
    def split_dir_and_basename(self, path: str) -> Tuple[str, str]:
        ...

    @abstractmethod
    def normpath(self, path: str) -> str:
        ...

    @abstractmethod
    def get_implementation_name_and_version_prefix(self) -> Tuple[str, str]:
        ...

    @abstractmethod
    def get_mpy_cross_args(self) -> List[str]:
        ...


class BaseAdapter(Adapter, ABC):
    def __init__(self):
        self._ensured_directories = set()
        self._sys_path: Optional[List[str]] = None
        self._sys_implementation: Optional[Tuple[str, str, int]] = None

    def get_sys_path(self) -> List[str]:
        if self._sys_path is None:
            self._sys_path = self.fetch_sys_path()
        return self._sys_path

    def get_implementation_name_and_version_prefix(self) -> Tuple[str, str]:
        impl = self.get_sys_implementation()
        return impl[0], ".".join(impl[1].split(".")[:2])

    def get_mpy_cross_args(self) -> List[str]:
        impl = self.get_sys_implementation()
        sys_mpy = impl[2]
        if sys_mpy is None:
            return []

        # https://docs.micropython.org/en/latest/reference/mpyfiles.html#versioning-and-compatibility-of-mpy-files
        args = []
        arch = [
            None,
            "x86",
            "x64",
            "armv6",
            "armv6m",
            "armv7m",
            "armv7em",
            "armv7emsp",
            "armv7emdp",
            "xtensa",
            "xtensawin",
        ][sys_mpy >> 10]
        if arch:
            args.append("-march=" + arch)
        if not sys_mpy & 0x200:
            args.append("-mno-unicode")

        return args

    @abstractmethod
    def fetch_sys_path(self) -> List[str]:
        ...

    def get_sys_implementation(self) -> Tuple[str, str, int]:
        if self._sys_implementation is None:
            self._sys_implementation = self.fetch_sys_implementation()
        return self._sys_implementation

    @abstractmethod
    def fetch_sys_implementation(self) -> Tuple[str, str, int]:
        ...

    @abstractmethod
    def remove_file_if_exists(self, path: str) -> None:
        ...

    @abstractmethod
    def remove_dir_if_empty(self, path: str) -> None:
        ...

    @abstractmethod
    def list_meta_dir_names(self, path: str, dist_name: Optional[str] = None) -> List[str]:
        """Return meta dir names from the indicated directory"""
        ...

    def get_default_target(self) -> str:
        sys_path = self.get_sys_path()
        # M5-Flow 2.0.0 has both /lib and /flash/libs
        for candidate in ["/flash/lib", "/flash/libs", "/lib"]:
            if candidate in sys_path:
                return candidate

        for entry in sys_path:
            if "lib" in entry:
                return entry
        raise AssertionError("Could not determine default target")

    def list_dists(self, paths: List[str] = None) -> Dict[str, Tuple[str, str]]:
        if not paths:
            paths = [entry for entry in self.get_sys_path() if entry.startswith("/")]

        result = {}
        for path in paths:
            for dir_name in self.list_meta_dir_names(path):
                dist_name, _ = parse_meta_dir_name(dir_name)
                if dist_name not in result:
                    result[dist_name] = dir_name, path

        return result

    def remove_dist(
        self, dist_name: str, target: Optional[str] = None, above_target: bool = False
    ) -> None:
        could_remove = False
        if target:
            result = self.check_remove_dist_from_path(dist_name, target)
            could_remove = could_remove or result
            if above_target and target in self.get_sys_path():
                for entry in self.get_sys_path():
                    if entry == "":
                        continue
                    elif entry == target:
                        break
                    else:
                        result = self.check_remove_dist_from_path(dist_name, entry)
                        could_remove = could_remove or result

        else:
            for entry in self.get_sys_path():
                if entry.startswith("/"):
                    result = self.check_remove_dist_from_path(dist_name, entry)
                    could_remove = could_remove or result
                    if result:
                        break

        if not could_remove:
            logger.warning("Could not find %r for removing", dist_name)

    def check_remove_dist_from_path(self, dist_name: str, path: str) -> bool:
        meta_dirs = self.list_meta_dir_names(path, dist_name)
        result = False
        for meta_dir_name in meta_dirs:
            self.remove_dist_by_meta_dir(path, meta_dir_name)
            result = True

        return result

    def remove_dist_by_meta_dir(self, containing_dir: str, meta_dir_name: str) -> None:
        record_bytes = self.read_file(self.join_path(containing_dir, meta_dir_name, "RECORD"))
        record_lines = record_bytes.decode(META_ENCODING).splitlines()

        package_dirs = set()
        for line in record_lines:
            rel_path, _, _ = line.split(",")
            abs_path = self.join_path(containing_dir, rel_path)
            logger.debug("Removing file %s", abs_path)
            self.remove_file_if_exists(abs_path)
            abs_dir, _ = self.split_dir_and_basename(abs_path)
            while len(abs_dir) > len(containing_dir):
                package_dirs.add(abs_dir)
                abs_dir, _ = self.split_dir_and_basename(abs_dir)

        for abs_dir in sorted(package_dirs, reverse=True):
            self.remove_dir_if_empty(abs_dir)

    def join_path(self, *parts: str) -> str:
        assert parts
        return self.get_dir_sep().join(parts)

    def split_dir_and_basename(self, path: str) -> Tuple[str, str]:
        dir_name, basename = path.rsplit(self.get_dir_sep(), maxsplit=1)
        return dir_name or None, basename or None

    def normpath(self, path: str) -> str:
        return path.replace("\\", self.get_dir_sep()).replace("/", self.get_dir_sep())

    @abstractmethod
    def get_dir_sep(self) -> str:
        ...

    def write_file(self, path: str, content: bytes) -> None:
        parent, _ = self.split_dir_and_basename(path)
        self.ensure_dir_exists(parent)
        self.write_file_in_existing_dir(path, content)

    def ensure_dir_exists(self, path: str) -> None:
        if (
            path in self._ensured_directories
            or path == "/"
            or path.endswith(":")
            or path.endswith(":\\")
        ):
            return
        else:
            parent, _ = self.split_dir_and_basename(path)
            if parent:
                self.ensure_dir_exists(parent)
            self.mkdir_in_existing_parent_exists_ok(path)
            self._ensured_directories.add(path)

    @abstractmethod
    def write_file_in_existing_dir(self, path: str, content: bytes) -> None:
        ...

    @abstractmethod
    def mkdir_in_existing_parent_exists_ok(self, path: str) -> None:
        ...


class InterpreterAdapter(BaseAdapter, ABC):
    """Base class for adapters, which communicate with an interpreter"""

    def __init__(self, executable: str):
        super(InterpreterAdapter, self).__init__()
        self._executable = executable


class ExecutableAdapter(InterpreterAdapter, ABC):
    def get_dir_sep(self) -> str:
        return os.path.sep


class LocalExecutableAdapter(ExecutableAdapter):
    ...


class SshExecutableAdapter(ExecutableAdapter):
    ...


class LocalMirrorAdapter(BaseAdapter, ABC):
    def __init__(self, base_path: str):
        super(LocalMirrorAdapter, self).__init__()
        self.base_path = base_path

    def get_user_packages_path(self) -> Optional[str]:
        return None

    def get_dir_sep(self) -> str:
        return "/"

    def get_mpy_cross_args(self) -> List[str]:
        return []

    def read_file(self, path: str) -> bytes:
        local_path = self.convert_to_local_path(path)
        with open(local_path, "rb") as fp:
            return fp.read()

    def write_file_in_existing_dir(self, path: str, content: bytes) -> None:
        local_path = self.convert_to_local_path(path)
        assert not os.path.isdir(local_path)

        block_size = 4 * 1024
        with open(local_path, "wb") as fp:
            while content:
                block = content[:block_size]
                content = content[block_size:]
                bytes_written = fp.write(block)
                fp.flush()
                os.fsync(fp)
                assert bytes_written == len(block)

    def remove_file_if_exists(self, path: str) -> None:
        local_path = self.convert_to_local_path(path)
        if os.path.exists(local_path):
            os.remove(local_path)

    def remove_dir_if_empty(self, path: str) -> None:
        local_path = self.convert_to_local_path(path)
        assert os.path.isdir(local_path)
        content = os.listdir(local_path)
        if not content:
            os.rmdir(local_path)

    def mkdir_in_existing_parent_exists_ok(self, path: str) -> None:
        local_path = self.convert_to_local_path(path)
        if not os.path.isdir(local_path):
            assert not os.path.exists(local_path)
            os.mkdir(local_path, 0o755)

    def convert_to_local_path(self, device_path: str) -> str:
        assert device_path.startswith("/")
        return os.path.normpath(self.base_path + device_path)

    def list_meta_dir_names(self, path: str, dist_name: Optional[str] = None) -> List[str]:
        local_path = self.convert_to_local_path(path)
        try:
            return [
                name
                for name in os.listdir(local_path)
                if name.endswith(".dist-info")
                and (dist_name is None or name.startswith(dist_name + "-"))
            ]
        except FileNotFoundError:
            # skipping non-existing dirs
            return []


class MountAdapter(LocalMirrorAdapter):
    def __init__(self, base_path: str):
        super().__init__(base_path)
        if not os.path.exists(base_path):
            raise UserError(f"Can't find mount point {base_path}")
        if os.path.isfile(base_path):
            raise UserError(f"Mount point {base_path} can't be a file")

        self._circuitpython_version = self._infer_cp_version()

    def fetch_sys_path(self) -> List[str]:
        if os.path.isdir(os.path.join(self.base_path, "lib")) or self.is_circuitpython():
            return ["", "/", ".frozen", "/lib"]
        elif os.path.isdir(os.path.join(self.base_path, "flash")):
            return ["", "/flash", "/flash/lib"]
        else:
            return ["", "/", ".frozen", "/lib"]

    def is_circuitpython(self) -> bool:
        # TODO: better look into the file as well
        return os.path.isfile(os.path.join(self.base_path, "boot_out.txt"))

    def _infer_cp_version(self) -> Optional[str]:
        boot_out_path = os.path.join(self.base_path, "boot_out.txt")
        if os.path.exists(boot_out_path):
            with open(boot_out_path) as fp:
                firmware_info = fp.readline().strip()
            match = re.match(r".*?CircuitPython (\d+\.\d+)\..+?", firmware_info)
            if match:
                return match.group(1)

        return None


class DirAdapter(LocalMirrorAdapter):
    def __init__(self, base_path: str):
        super().__init__(base_path)
        if not os.path.isdir(base_path):
            assert not os.path.exists(base_path)
            os.makedirs(base_path, mode=0o755)

    def fetch_sys_path(self) -> List[str]:
        # This means, list command without --path will consider this directory
        return ["/"]

    def fetch_sys_implementation(self) -> Tuple[str, str, int]:
        # TODO:
        return ("micropython", "1.18", 0)

    def get_default_target(self) -> str:
        return "/"


def create_adapter(port: Optional[str], mount: Optional[str], dir: Optional[str], **kw) -> Adapter:
    if port:
        from pipkin import serial_connection
        from pipkin import bare_metal

        connection = serial_connection.SerialConnection(port)
        return bare_metal.SerialPortAdapter(connection)
    elif dir:
        return DirAdapter(dir)
    elif mount:
        return MountAdapter(mount)
    else:
        raise NotImplementedError()
