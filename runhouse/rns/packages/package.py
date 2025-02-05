import copy
import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional, Union

from runhouse import rh_config
from runhouse.rns.folders.folder import Folder
from runhouse.rns.resource import Resource
from runhouse.rns.utils.hardware import _get_cluster_from

INSTALL_METHODS = {"local", "reqs", "pip", "conda"}


class Package(Resource):
    RESOURCE_TYPE = "package"

    # https://pytorch.org/get-started/locally/
    # Note: no binaries exist for 11.4 (https://github.com/pytorch/pytorch/issues/75992)
    TORCH_INDEX_URLS = {
        "11.3": "https://download.pytorch.org/whl/cu113",
        "11.5": "https://download.pytorch.org/whl/cu115",
        "11.6": "https://download.pytorch.org/whl/cu116",
        "11.7": "https://download.pytorch.org/whl/cu117",
        "11.8": "https://download.pytorch.org/whl/cu118",
        "cpu": "https://download.pytorch.org/whl/cpu",
    }

    def __init__(
        self,
        name: str = None,
        install_method: str = None,
        install_target: Union[str, Folder] = None,
        install_args: str = None,
        dryrun: bool = False,
        **kwargs,  # We have this here to ignore extra arguments when calling from from_config
    ):
        """
        Runhouse Package resource.
        """
        super().__init__(
            name=name,
            dryrun=dryrun,
        )
        self.install_method = install_method
        self.install_target = install_target
        self.install_args = install_args

    @property
    def config_for_rns(self):
        # If the package is just a simple Package.from_string string, no
        # need to store it in rns, just give back the string.
        # if self.install_method in ['pip', 'conda', 'git']:
        #     return f'{self.install_method}:{self.name}'
        config = super().config_for_rns
        config["install_method"] = self.install_method
        config["install_target"] = self._resource_string_for_subconfig(
            self.install_target
        )
        config["install_args"] = self.install_args
        return config

    def __str__(self):
        if self.name:
            return f"Package: {self.name}"
        if isinstance(self.install_target, Folder):
            # if self.install_target.name:
            #     return f'Package: {self.install_target.name}'
            return f"Package: {self.install_target.path}"
        return f"Package: {self.install_target}"

    def install(self, env: Union[str, "Env"] = None):
        """Install package.

        Args:
            env (Env or str): Environment to install package on. If left empty, defaults to base environment.
                (Default: ``None``)
        """
        logging.info(
            f"Installing package {str(self)} with method {self.install_method}."
        )
        install_cmd = ""
        install_args = f" {self.install_args}" if self.install_args else ""
        cuda_version_or_cpu = self.detect_cuda_version_or_cpu()

        if isinstance(self.install_target, Folder):
            local_path = self.install_target.local_path
            if self.install_method == "pip":
                # TODO [DG] Revisit: Would be nice if we could use -e by default, but importlib on the grpc server
                #  isn't finding the package right after its installed.
                # if (Path(local_path) / 'setup.py').exists():
                #     install_cmd = f'-e {local_path}' + install_args
                # else:
                install_cmd = f"{local_path}" + install_args
            elif self.install_method == "conda":
                install_cmd = f"{local_path}" + install_args
            elif self.install_method == "reqs":
                reqs_path = f"{local_path}/requirements.txt"
                logging.info(f"reqs path: {reqs_path}")
                if Path(reqs_path).expanduser().exists():
                    # Ensure each requirement listed in the file contains the full install command for torch packages
                    reqs_from_file: list = self.format_torch_cmd_in_reqs_file(
                        path=reqs_path, cuda_version_or_cpu=cuda_version_or_cpu
                    )
                    logging.info(f"Got reqs from file to install: {reqs_from_file}")

                    # Format URLs for any torch packages listed in the requirements.txt file
                    logging.info(
                        f"Attempting to install formatted requirements from {reqs_path} "
                    )

                    self.pip_install(install_cmd=" ".join(reqs_from_file))

                else:
                    logging.info(f"{local_path}/requirements.txt not found, skipping")
        else:
            install_cmd = self.install_target + install_args

        if self.install_method == "pip":
            install_cmd = self.install_cmd_for_torch(install_cmd, cuda_version_or_cpu)
            if not install_cmd:
                raise ValueError("Invalid install command")

            self.pip_install(install_cmd, env)
        elif self.install_method == "conda":
            self.conda_install(install_cmd, env)
        elif self.install_method in ["local", "reqs"]:
            if isinstance(self.install_target, Folder):
                sys.path.append(local_path)
            elif Path(self.install_target).resolve().expanduser().exists():
                sys.path.append(str(Path(self.install_target).resolve().expanduser()))
            else:
                raise ValueError(
                    f"install_target must be a Folder or a path to a directory for "
                    f"install_method {self.install_method}"
                )
        # elif self.install_method == 'unpickle':
        #     # unpickle the functions and make them importable
        #     with self.get('functions.pickle') as f:
        #         sys.modules[self.name] = pickle.load(f)
        else:
            raise ValueError(
                f"Unknown install_method {self.install_method}. Try using cluster.run() or "
                f"function.run_setup() to install instead."
            )

    # ----------------------------------
    # Torch Install Helpers
    # ----------------------------------
    def format_torch_cmd_in_reqs_file(self, path, cuda_version_or_cpu):
        """Read requirements from file, append --index-url and --extra-index-url where relevant for torch packages,
        and return list of formatted packages."""
        with open(path) as f:
            reqs = f.readlines()

        # Leave the file alone, maintain a separate list with the updated commands which we will later pip install
        reqs_from_file = []
        for req in reqs:
            install_cmd = self.install_cmd_for_torch(req.strip(), cuda_version_or_cpu)
            reqs_from_file.append(install_cmd)

        return reqs_from_file

    def install_cmd_for_torch(self, install_cmd, cuda_version_or_cpu):
        """Return the correct formatted pip install command for the torch package(s) provided."""
        torch_source_packages = ["torch", "torchvision", "torchaudio"]
        if not any([x in install_cmd for x in torch_source_packages]):
            return install_cmd

        packages_to_install: list = self.packages_to_install_from_cmd(install_cmd)
        final_install_cmd = ""
        for package_install_cmd in packages_to_install:
            formatted_cmd = self._install_url_for_torch_package(
                package_install_cmd, cuda_version_or_cpu
            )
            if formatted_cmd:
                final_install_cmd += formatted_cmd + " "

        final_install_cmd = final_install_cmd.rstrip()
        return final_install_cmd if final_install_cmd != "" else None

    def _install_url_for_torch_package(self, install_cmd, cuda_version_or_cpu):
        """Build the full install command, adding a --index-url and --extra-index-url where applicable."""
        # Grab the relevant index url for torch based on the CUDA version provided
        if "," in install_cmd:
            # If installing a range of versions format the string to make it compatible with `pip_install` method
            install_cmd = install_cmd.replace(" ", "")

        index_url = self.torch_index_url(cuda_version_or_cpu)
        if index_url and not any(
            specifier in install_cmd for specifier in ["--index-url ", "-i "]
        ):
            install_cmd = f"{install_cmd} --index-url {index_url}"

        if "--extra-index-url" not in install_cmd:
            return f"{install_cmd} --extra-index-url https://pypi.python.org/simple/"

        return install_cmd

    def torch_index_url(self, cuda_version_or_cpu: str):
        return self.TORCH_INDEX_URLS.get(cuda_version_or_cpu)

    @staticmethod
    def detect_cuda_version_or_cpu():
        """Return the CUDA version on the cluster. If we are on a CPU-only cluster return 'cpu'.

        Note: A cpu-only machine may have the CUDA toolkit installed, which means nvcc will still return
        a valid version. Also check if the NVIDIA driver is installed to confirm we are on a GPU."""
        try:
            cuda_version_info: str = subprocess.check_output(
                "nvcc --version", shell=True
            ).decode("utf-8")
            cuda_version = cuda_version_info.split("release ")[1].split(",")[0]
        except subprocess.CalledProcessError:
            return "cpu"

        try:
            subprocess.check_call(["nvidia-smi"])
            return cuda_version
        except subprocess.CalledProcessError:
            return "cpu"

    @staticmethod
    def packages_to_install_from_cmd(install_cmd: str):
        """Split a string of command(s) into a list of separate commands"""
        # Remove any --extra-index-url flags from the install command (to be added later by default)
        install_cmd = re.sub(r"--extra-index-url\s+\S+", "", install_cmd)
        install_cmd = install_cmd.strip()

        if ", " in install_cmd:
            # Ex: 'torch>=1.13.0,<2.0.0'
            return [install_cmd]

        matches = re.findall(r"(\S+(?:\s+(-i|--index-url)\s+\S+)?)", install_cmd)

        packages_to_install = [match[0] for match in matches]
        return packages_to_install

    # ----------------------------------

    @staticmethod
    def pip_install(install_cmd: str, env: Union[str, "Env"] = ""):
        """Run pip install."""
        if env:
            if isinstance(env, str):
                from runhouse.rns.envs import Env

                env = Env.from_name(env)
            cmd_prefix = env._run_cmd
        else:
            cmd_prefix = f"{sys.executable} -m"
        cmd = f"{cmd_prefix} pip install {install_cmd}"
        logging.info(f"Running: {cmd}")
        subprocess.check_call(cmd.split(" "))

    @staticmethod
    def conda_install(install_cmd: str, env: Union[str, "Env"] = ""):
        """Run conda install."""
        cmd = f"conda install -y {install_cmd}"
        if env:
            if isinstance(env, str):
                from runhouse.rns.envs import Env

                env = Env.from_name(env)
            cmd = f"{env._run_cmd} {cmd}"
        logging.info(f"Running: {cmd}")
        # check if conda is installed, and if not, install it
        try:
            subprocess.check_call(["conda", "--version"])
            subprocess.run(cmd.split(" "))
        except FileNotFoundError:
            logging.info("Conda not found, installing...")
            subprocess.check_call(
                "wget https://repo.continuum.io/miniconda/Miniconda3-latest-Linux-x86_64.sh "
                "-O ~/miniconda.sh".split(" ")
            )
            subprocess.check_call(["bash", "~/miniconda.sh", "-b", "-p", "~/miniconda"])
            subprocess.check_call("source $HOME/miniconda3/bin/activate".split(" "))
            status = subprocess.check_call(cmd.split(" "))
            if not status == 0:
                raise RuntimeError(
                    "Conda install failed, check that the package exists and is "
                    "available for your platform."
                )

    def to(
        self,
        system: Union[str, Dict, "Cluster"],
        path: Optional[str] = None,
        mount: bool = False,
    ):
        """Copy the package onto filesystem or cluster, and return the new Package object."""
        if not isinstance(self.install_target, Folder):
            raise TypeError(
                "`install_target` must be a Folder in order to copy the package to a system."
            )

        system = _get_cluster_from(system)
        if self.install_target.system == system:
            return self

        if isinstance(system, Resource):
            new_folder = self.install_target.to_cluster(system, path=path, mount=mount)
        else:  # to fs
            new_folder = self.install_target.to(system, path=path)
        new_folder.system = system
        new_package = copy.copy(self)
        new_package.install_target = new_folder
        return new_package

    @staticmethod
    def from_config(config: dict, dryrun=False):
        if isinstance(config.get("install_target"), dict):
            config["install_target"] = Folder.from_config(config["install_target"])
        if config.get("resource_subtype") == "GitPackage":
            from runhouse import GitPackage

            return GitPackage.from_config(config, dryrun=dryrun)
        return Package(**config, dryrun=dryrun)

    @staticmethod
    def from_string(specifier: str, dryrun=False):
        # Use regex to check if specifier matches '<method>:https://github.com/<path>' or 'https://github.com/<path>'
        match = re.search(
            r"^(?:(?P<method>[^:]+):)?(?P<path>https://github.com/.+)", specifier
        )
        if match:
            install_method = match.group("method")
            url = match.group("path")
            from runhouse.rns.packages.git_package import git_package

            return git_package(
                git_url=url, install_method=install_method, dryrun=dryrun
            )

        target_and_args = specifier
        if specifier.split(":")[0] in INSTALL_METHODS:
            target_and_args = specifier.split(":", 1)[1]
        rel_target, args = (
            target_and_args.split(" ", 1)
            if " " in target_and_args
            else (target_and_args, "")
        )

        # We need to do this because relative paths are relative to the current working directory!
        abs_target = (
            Path(rel_target).expanduser()
            if Path(rel_target).expanduser().is_absolute()
            else Path(rh_config.rns_client.locate_working_dir()) / rel_target
        )
        if abs_target.exists():
            target = Folder(
                path=abs_target, dryrun=True
            )  # No need to create the folder here
        else:
            target = rel_target

        if specifier.startswith("local:"):
            return Package(install_target=target, install_method="local", dryrun=dryrun)
        elif specifier.startswith("reqs:"):
            return Package(
                install_target=target,
                install_args=args,
                install_method="reqs",
                dryrun=dryrun,
            )
        elif specifier.startswith("pip:"):
            return Package(
                install_target=target,
                install_args=args,
                install_method="pip",
                dryrun=dryrun,
            )
        elif specifier.startswith("conda:"):
            return Package(
                install_target=target,
                install_args=args,
                install_method="conda",
                dryrun=dryrun,
            )
        elif specifier.startswith("rh:"):
            # Calling the factory method below
            return package(name=specifier[3:], dryrun=dryrun)
        else:
            if Path(specifier).resolve().exists():
                return Package(
                    install_target=target,
                    install_args=args,
                    install_method="reqs",
                    dryrun=dryrun,
                )
            else:
                return Package(
                    install_target=target,
                    install_args=args,
                    install_method="pip",
                    dryrun=dryrun,
                )


def package(
    name: str = None,
    install_method: str = None,
    install_str: str = None,
    url: str = None,
    system: str = Folder.DEFAULT_FS,
    dryrun: bool = False,
    local_mount: bool = False,
    data_config: Optional[Dict] = None,
    load: bool = True,
) -> Package:
    """
    Builds an instance of :class:`Package`.

    Args:
        name (str): Name to assign the pacakge.
        install_method (str): Method for installing the package. Options: [``pip``, ``conda``, ``reqs``, ``local``]
        install_str (str): Additional arguments to install.
        url (str): URL of the package to install.
        system (str): File system. Currently this must be one of:
            [``file``, ``github``, ``sftp``, ``ssh``, ``s3``, ``gs``, ``azure``].
            We are working to add additional file system support.
        dryrun (bool): Whether to create the Package if it doesn't exist, or load the Package object as a dryrun.
            (Default: ``False``)
        local_mount (bool): Whether to locally mount the installed package. (Default: ``False``)
        data_config (Optional[Dict]): The data config to pass to the underlying fsspec handler.
        load (bool): Whether to load an existing config for the Package. (Default: ``True``)

    Returns:
        Package: The resulting package.
    """
    config = rh_config.rns_client.load_config(name) if load else {}
    config["name"] = name or config.get("rns_address", None) or config.get("name")

    config["install_method"] = install_method or config.get("install_method")
    if url is not None:
        config["install_target"] = Folder(
            path=url, system=system, local_mount=local_mount, data_config=data_config
        )
        config["install_args"] = install_str
    elif install_str is not None:
        config["install_target"], config["install_args"] = install_str.split(" ", 1)
    elif "install_target" in config and isinstance(config["install_target"], dict):
        config["install_target"] = Folder.from_config(config["install_target"])

    new_package = Package.from_config(config, dryrun=dryrun)

    return new_package
