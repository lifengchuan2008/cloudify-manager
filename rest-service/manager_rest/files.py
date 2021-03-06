#########
# Copyright (c) 2015 GigaSpaces Technologies Ltd. All rights reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
#  * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  * See the License for the specific language governing permissions and
#  * limitations under the License.
import json
import uuid

import yaml
from setuptools import archive_util

import contextlib
import os
import shutil
import tempfile
from urllib2 import urlopen, URLError

from flask import request

from manager_rest import config, chunked, manager_exceptions


class UploadedDataManager(object):

    def receive_uploaded_data(self, data_id):
        file_server_root = config.instance().file_server_root
        resource_target_path = tempfile.mktemp(dir=file_server_root)
        try:
            additional_inputs = self._save_file_locally_and_extract_inputs(
                    resource_target_path,
                    self._get_data_url_key(),
                    self._get_kind())
            doc, dest_file_name = self._prepare_and_process_doc(
                data_id,
                file_server_root,
                resource_target_path,
                additional_inputs=additional_inputs)
            self._move_archive_to_uploaded_dir(doc.id,
                                               file_server_root,
                                               resource_target_path,
                                               dest_file_name=dest_file_name)

            return doc, 201
        finally:
            if os.path.exists(resource_target_path):
                os.remove(resource_target_path)

    @classmethod
    def _extract_file_to_file_server(cls, archive_path, destination_root):
        """
        Extracting a package.

        :param destination_root: the root destination for the unzipped archive
        :param archive_path: the archive path
        :return: the full path for the extracted archive
        """
        # Importing this archives in the global scope causes import loop
        from manager_rest.resources import SUPPORTED_ARCHIVE_TYPES
        # extract application to file server
        tempdir = tempfile.mkdtemp('-blueprint-submit')
        try:
            try:
                archive_util.unpack_archive(archive_path, tempdir)
            except archive_util.UnrecognizedFormat:
                raise manager_exceptions.BadParametersError(
                        'Blueprint archive is of an unrecognized format. '
                        'Supported formats are: {0}'.format(
                                SUPPORTED_ARCHIVE_TYPES))
            archive_file_list = os.listdir(tempdir)
            if len(archive_file_list) != 1 or not os.path.isdir(
                    os.path.join(tempdir, archive_file_list[0])):
                raise manager_exceptions.BadParametersError(
                        'archive must contain exactly 1 directory')
            application_dir_base_name = archive_file_list[0]
            # generating temporary unique name for app dir, to allow multiple
            # uploads of apps with the same name (as it appears in the file
            # system, not the app name field inside the blueprint.
            # the latter is guaranteed to be unique).
            generated_app_dir_name = '{0}-{1}'.format(
                    application_dir_base_name, uuid.uuid4())
            temp_application_dir = os.path.join(tempdir,
                                                application_dir_base_name)
            temp_application_target_dir = os.path.join(tempdir,
                                                       generated_app_dir_name)
            shutil.move(temp_application_dir, temp_application_target_dir)
            shutil.move(temp_application_target_dir, destination_root)
            return generated_app_dir_name
        finally:
            shutil.rmtree(tempdir)

    @staticmethod
    def _save_file_from_url(archive_target_path, data_url, data_type):
        if any([request.data,
                'Transfer-Encoding' in request.headers,
                'blueprint_archive' in request.files]):
            raise manager_exceptions.BadParametersError(
                "Can't pass both a {0} URL via query parameters, request body"
                ", multi-form and chunked.".format(data_type))
        try:
            with contextlib.closing(urlopen(data_url)) as urlf:
                with open(archive_target_path, 'w') as f:
                    f.write(urlf.read())
        except URLError:
            raise manager_exceptions.ParamUrlNotFoundError(
                    "URL {0} not found - can't download {1} archive"
                    .format(data_url, data_type))
        except ValueError:
            raise manager_exceptions.BadParametersError(
                    "URL {0} is malformed - can't download {1} archive"
                    .format(data_url, data_type))

    @staticmethod
    def _save_file_from_chunks(archive_target_path, data_type):
        if any([request.data,
                'blueprint_archive' in request.files]):
            raise manager_exceptions.BadParametersError(
                "Can't pass both a {0} URL via request body , multi-form "
                "and chunked.".format(data_type))
        with open(archive_target_path, 'w') as f:
            for buffered_chunked in chunked.decode(request.input_stream):
                f.write(buffered_chunked)

    @staticmethod
    def _save_file_content(archive_target_path, data_type, url_key):
        if 'blueprint_archive' in request.files:
            raise manager_exceptions.BadParametersError(
                "Can't pass both a {0} URL via request body , multi-form"
                .format(data_type))
        uploaded_file_data = request.data
        with open(archive_target_path, 'w') as f:
            f.write(uploaded_file_data)

    def _save_files_multipart(self, archive_target_path):
        inputs = {}
        for file_key in request.files:
            if file_key == 'inputs':
                content = request.files[file_key]
                # The file is a binary
                if 'application' in content.content_type:
                    content_payload = self._save_bytes(content)
                    # Handling yaml
                    if content.content_type == 'application/octet-stream':
                        inputs = yaml.load(content_payload)
                    # Handling json
                    elif content.content_type == 'application/json':
                        inputs = json.load(content_payload)
                # The file is raw json
                elif 'text' in content.content_type:
                    inputs = json.load(content)
            elif file_key == 'blueprint_archive':
                self._save_bytes(request.files[file_key],
                                 archive_target_path)
        return inputs

    @staticmethod
    def _save_bytes(content, target_path=None):
        """
        content should support read() function if target isn't supplied,
        string rep is returned

        :param content:
        :param target_path:
        :return:
        """
        if not target_path:
            return content.getvalue().decode("utf-8")
        else:
            with open(target_path, 'wb') as f:
                f.write(content.read())

    def _save_file_locally_and_extract_inputs(self,
                                              archive_target_path,
                                              url_key,
                                              data_type='unknown'):
        """
        Retrieves the file specified by the request to the local machine.

        :param archive_target_path: the target of the archive
        :param data_type: the kind of the data (e.g. 'blueprint')
        :param url_key: if the data is passed as a url to an online resource,
        the url_key specifies what header points to the requested url.
        :return: None
        """
        inputs = {}

        # Handling importing blueprint through url
        if url_key in request.args:
            self._save_file_from_url(archive_target_path,
                                     request.args[url_key],
                                     data_type)
        # handle receiving chunked blueprint
        elif 'Transfer-Encoding' in request.headers:
            self._save_file_from_chunks(archive_target_path, data_type)
        # handler receiving entire content through data
        elif request.data:
            self._save_file_content(archive_target_path, url_key, data_type)

        # handle inputs from form-data (for both the blueprint and inputs
        # in body in form-data format)
        if request.files:
            inputs = self._save_files_multipart(archive_target_path)

        return inputs

    def _move_archive_to_uploaded_dir(self,
                                      data_id,
                                      root_path,
                                      archive_path,
                                      dest_file_name=None):
        if not os.path.exists(archive_path):
            raise RuntimeError("Archive [{0}] doesn't exist - Cannot move "
                               "archive to uploaded {1}s "
                               "directory".format(archive_path,
                                                  self._get_kind()))
        uploaded_dir = os.path.join(
            root_path,
            self._get_target_dir_path(),
            data_id)
        if not os.path.isdir(uploaded_dir):
            os.makedirs(uploaded_dir)
        archive_type = self._get_archive_type(archive_path)
        if not dest_file_name:
            dest_file_name = '{0}.{1}'.format(data_id, archive_type)
        shutil.move(archive_path,
                    os.path.join(uploaded_dir, dest_file_name))

    def _get_kind(self):
        raise NotImplementedError('Subclass responsibility')

    def _get_data_url_key(self):
        raise NotImplementedError('Subclass responsibility')

    def _get_target_dir_path(self):
        raise NotImplementedError('Subclass responsibility')

    def _get_archive_type(self, archive_path):
        raise NotImplementedError('Subclass responsibility')

    def _prepare_and_process_doc(self, data_id, file_server_root,
                                 archive_target_path, additional_inputs):
        raise NotImplementedError('Subclass responsibility')
