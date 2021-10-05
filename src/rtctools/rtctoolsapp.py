import logging
import os
import shutil
import sys
from pathlib import Path

import rtctools

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("rtctools")
logger.setLevel(logging.INFO)


def copy_libraries(*args):

    if not args:
        args = sys.argv[1:]

    if not args:
        path = input("Folder to put the Modelica libraries: [.] ") or "."
    else:
        path = args[0]

    if not os.path.exists(path):
        sys.exit(f"Folder '{path}' does not exist")

    # pkg_resources can be quite a slow import, so we do it here
    import pkg_resources

    def _copytree(src, dst, symlinks=False, ignore=None):
        if not os.path.exists(dst):
            os.makedirs(dst)
        for item in os.listdir(src):
            s = os.path.join(src, item)
            d = os.path.join(dst, item)
            if os.path.isdir(s):
                _copytree(s, d, symlinks, ignore)
            else:
                if not os.path.exists(d):
                    shutil.copy2(s, d)
                elif Path(s).name.lower() == "package.mo":
                    # Pick the largest one, assuming that all plugin packages
                    # to not provide a meaningful package.mo
                    if os.stat(s).st_size > os.stat(d).st_size:
                        logger.warning(f"Overwriting '{d}' with '{s}' as the latter is larger.")
                        os.remove(d)
                        shutil.copy2(s, d)
                    else:
                        logger.warning(f"Not copying '{s}' to '{d}' as the latter is larger.")
                else:
                    raise OSError("Could not combine two folders")

    dst = Path(path)

    library_folders = []
    for ep in pkg_resources.iter_entry_points(group="rtctools.libraries.modelica"):
        if ep.name == "library_folder":
            library_folders.append(
                Path(pkg_resources.resource_filename(ep.module_name, ep.attrs[0]))
            )

    tlds = {}
    for lf in library_folders:
        for x in lf.iterdir():
            if x.is_dir():
                tlds.setdefault(x.name, []).append(x)

    for tld, paths in tlds.items():
        if Path(tld).exists():
            sys.exit(f"Library with name '{tld}'' already exists")

        try:
            for p in paths:
                _copytree(p, dst / p.name)
        except OSError:
            sys.exit(f"Failed merging the libraries in package '{tld}'")

    sys.exit(f"Succesfully copied all library folders to '{dst.resolve()}'")


def download_examples(*args):

    if not args:
        args = sys.argv[1:]

    if not args:
        path = input("Folder to download the examples to: [.] ") or "."
    else:
        path = args[0]

    if not os.path.exists(path):
        sys.exit(f"Folder '{path}' does not exist")

    path = Path(path)

    import urllib.request
    from urllib.error import HTTPError
    from zipfile import ZipFile

    # GitLab is blocking requests unless we specify a user agent
    user_agent = "Mozilla/5.0 (Windows NT 6.1; Win64; x64; rv:47.0) Gecko/20100101 Firefox/47.0"

    version = rtctools.__version__
    rtc_full_name = f"rtc-tools-{version}"
    try:
        url = "https://gitlab.com/deltares/rtc-tools/-/archive/" "{}/{}.zip".format(
            version, rtc_full_name
        )

        opener = urllib.request.build_opener()
        opener.addheaders = [("User-agent", user_agent)]
        urllib.request.install_opener(opener)
        local_filename, _ = urllib.request.urlretrieve(url)
    except HTTPError:
        sys.exit(f"Could not found examples for RTC-Tools version {version}.")

    with ZipFile(local_filename, "r") as z:
        target = path / "rtc-tools-examples"
        prefix = f"{rtc_full_name}/examples/"
        members = [x for x in z.namelist() if x.startswith(prefix)]
        z.extractall(members=members)
        shutil.move(prefix, target)
        shutil.rmtree(rtc_full_name)

        sys.exit(f"Succesfully downloaded the RTC-Tools examples to '{target.resolve()}'")

    try:
        os.remove(local_filename)
    except OSError:
        pass
