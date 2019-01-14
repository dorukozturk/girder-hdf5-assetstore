from functools import partial
import h5py
import numpy as np
import os
from tempfile import TemporaryFile

from girder.api import access
from girder.api.describe import autoDescribeRoute, Description
from girder.api.rest import boundHandler, setResponseHeader
from girder.constants import AccessType, AssetstoreType, TokenScope
from girder.exceptions import AccessException, RestException
from girder.models.assetstore import Assetstore
from girder.models.file import File
from girder.models.folder import Folder
from girder.models.item import Item
from girder.utility.assetstore_utilities import (
    getAssetstoreAdapter,
    setAssetstoreAdapter,
)
from girder.utility.filesystem_assetstore_adapter import (
    FilesystemAssetstoreAdapter,
    BUF_SIZE,
)
from girder.utility.progress import ProgressContext


def get_corresponding_hdf5_obj(obj, token):
    while os.path.basename(obj.name) != token:
        obj = obj.parent
    return obj


def resolve_group(root_folder, obj, user):
    tokens = [i for i in obj.name.split("/") if i]
    parent = root_folder
    for token in tokens:
        hdf5_obj = get_corresponding_hdf5_obj(obj, token)
        parent = Folder().createFolder(
            parent, token, creator=user, reuseExisting=True
        )
        parent["hdf5Metadata"] = str(hdf5_obj.attrs.items())
        parent["pathInHdf5"] = hdf5_obj.name
        Folder().save(parent)


def resolve_dataset(root_folder, obj, user, assetstore, hdf5_path):
    directory, name = os.path.split(obj.name)
    tokens = [i for i in directory.split("/") if i]
    parent = root_folder
    for token in tokens:
        hdf5_obj = get_corresponding_hdf5_obj(obj, token)
        parent = Folder().createFolder(
            parent, token, creator=user, reuseExisting=True
        )
        parent["hdf5Metadata"] = str(hdf5_obj.attrs.items())
        parent["pathInHdf5"] = hdf5_obj.name
        Folder().save(parent)
    item = Item().createItem(
        name=name, creator=user, folder=parent, reuseExisting=True
    )
    item["hdf5Metadata"] = str(obj.attrs.items())
    Item().save(item)
    hdf = h5py.File(hdf5_path, "r")
    dataset = hdf.get(obj.name)
    girder_file = File().createFile(
        name=name,
        creator=user,
        item=item,
        reuseExisting=True,
        assetstore=assetstore,
        saveFile=True,
        size=dataset[()].nbytes,
    )
    girder_file["pathInHdf5"] = obj.name
    girder_file["hdf5Path"] = hdf5_path
    File().save(girder_file)


def mirror_objects_in_girder(
    folder, progress, user, assetstore, hdf5_path, name, obj
):
    progress.update(message=name)
    if isinstance(obj, h5py.Dataset):
        resolve_dataset(folder, obj, user, assetstore, hdf5_path)
    elif isinstance(obj, h5py.Group):
        resolve_group(folder, obj, user)


class Hdf5SupportAdapter(FilesystemAssetstoreAdapter):
    def _downloadFromHdf5(
        self, girder_file, offset, endByte, headers, contentDisposition
    ):
        if endByte is None or endByte > girder_file["size"]:
            endByte = girder_file["size"]

        if headers:
            setResponseHeader("Accept-Ranges", "bytes")
            self.setContentHeaders(
                girder_file, offset, endByte, contentDisposition
            )

        def stream():
            with h5py.File(girder_file["hdf5Path"], "r") as hdf5:
                dataset = hdf5.get(girder_file["pathInHdf5"])[()]
                fh = TemporaryFile()
                np.save(fh, dataset)
                fh.seek(0)
                bytesRead = offset

                if offset > 0:
                    fh.seek(offset)

                while True:
                    readLen = min(BUF_SIZE, endByte - bytesRead)
                    if readLen <= 0:
                        break

                    data = fh.read(readLen)
                    bytesRead += readLen

                    if not data:
                        break
                    yield data
                fh.close()

        return stream

    def downloadFile(
        self,
        girder_file,
        offset=0,
        headers=True,
        endByte=None,
        contentDisposition=None,
        **kwargs
    ):
        if girder_file.get("hdf5Path"):
            return self._downloadFromHdf5(
                girder_file,
                offset=0,
                headers=True,
                endByte=None,
                contentDisposition=None,
            )

        return super(Hdf5SupportAdapter, self).downloadFile(
            girder_file, offset, headers, endByte, contentDisposition, **kwargs
        )

    def _importHdf5(self, path, folder, progress, user):
        if not os.path.isabs(path):
            path = os.path.join(self.assetstore["root"], path)

        try:
            hdf = h5py.File(path, "r")
            hdf.visititems(
                partial(
                    mirror_objects_in_girder,
                    folder,
                    progress,
                    user,
                    self.assetstore,
                    path,
                )
            )
        except IOError:
            raise RestException("{} is not an hdf5 file".format(path))


@boundHandler
@access.admin(scope=TokenScope.DATA_WRITE)
@autoDescribeRoute(
    Description("Import an hdf5 file into the system.")
    .notes(
        "This does not move or copy the existing data, it just creates "
        "references to it in the Girder data hierarchy. Deleting "
        "those references will not delete the underlying data."
    )
    .modelParam("id", model=Assetstore)
    .modelParam(
        "folderId",
        "Import destination folder.",
        model=Folder,
        level=AccessType.WRITE,
        paramType="formData",
    )
    .param("path", "Path of the hdf file to import.")
    .param(
        "progress",
        "Whether to record progress on the import.",
        dataType="boolean",
        default=False,
        required=False,
    )
    .errorResponse()
    .errorResponse("You are not an administrator.", 403)
)
def _importHdf5(self, assetstore, folder, path, progress):
    user = self.getCurrentUser()
    adapter = getAssetstoreAdapter(assetstore)
    with ProgressContext(progress, user=user, title="Importing data") as ctx:
        adapter._importHdf5(path, folder, ctx, user)


def load(info):
    setAssetstoreAdapter(AssetstoreType.FILESYSTEM, Hdf5SupportAdapter)
    info["apiRoot"].assetstore.route(
        "POST", (":id", "hdf5_import"), _importHdf5
    )
