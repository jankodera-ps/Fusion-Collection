# -*- coding: utf-8 -*-

# (c) 2023, Jan Kodera (jkodera@purestorage.com)
# GNU General Public License v3.0+ (see COPYING.GPLv3 or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import absolute_import, division, print_function

__metaclass__ = type

try:
    import fusion as purefusion
except ImportError:
    pass

import sys
import json
import re


class OperationException(Exception):
    """Raised if an asynchronous Operation fails."""

    def __init__(self, op):
        self._op = op

    @property
    def op(self):
        return self._op


def _should_be_verbose(module) -> bool:
    # verbosity is a private member and Ansible does not really allow
    # providing extra information only if the user wants it due to ideological
    # reasons, so extract it as carefully as possible and assume non-verbose
    # if something fails
    try:
        if module._verbosity is not None and isinstance(module._verbosity, int):
            return module._verbosity > 0
    except Exception:
        pass
    return False


def _extract_rest_call_site(traceback):
    # extracts first function in traceback that comes from 'fusion.api.*_api',
    # converts its name from something like 'get_volume' to 'Get volume' and returns
    while traceback is not None:
        try:
            frame = traceback.tb_frame
            func_name = (
                frame.f_code.co_name
            )  # contains function name, e.g. 'get_volume'
            mod_path = frame.f_globals[
                "__name__"
            ]  # contains module path, e.g. 'fusion.api.volumes_api'
            path_segments = mod_path.split(".")
            if (
                path_segments[0] == "fusion"
                and path_segments[1] == "api"
                and "_api" in path_segments[2]
            ):
                call_site = func_name.replace("_", " ").capitalize()
                return call_site
        except Exception:
            pass
        traceback = traceback.tb_next
    return None


def format_fusion_api_exception(exception, traceback=None):
    """Formats `fusion.rest.ApiException` into a simple short form, suitable
    for Ansible error output. Returns a (message: str, body: dict) tuple."""
    message = None
    code = None
    resource_name = None
    request_id = None
    body = None
    call_site = _extract_rest_call_site(traceback)
    try:
        body = json.loads(exception.body)
        request_id = body.get("request_id", None)
        error = body["error"]
        message = error.get("message")
        code = error.get("pure_code")
        if not code:
            code = exception.status
        if not code:
            code = error.get("http_code")
        resource_name = error["details"]["name"]
    except Exception:
        pass

    output = ""
    if call_site:
        output += "'{0}' failed".format(call_site)
    else:
        output += "request failed"

    if message:
        output += ", {0}".format(message.replace('"', "'"))

    parenthesed = False
    if resource_name:
        if not parenthesed:
            parenthesed = True
            output += " ("
        else:
            output += ", "
        output += "resource: '{0}'".format(resource_name)
    if code:
        if not parenthesed:
            parenthesed = True
            output += " ("
        else:
            output += ", "
        output += "code: '{0}'".format(code)
    if request_id:
        if not parenthesed:
            output += " ("
            parenthesed = True
        else:
            output += ", "
        output += "request id: '{0}'".format(request_id)
    if parenthesed:
        output += ")"

    return (output, body)


def format_failed_fusion_operation(op):
    """Formats failed `fusion.Operation` into a simple short form, suitable
    for Ansible error output. Returns a (message: str, body: dict) tuple."""
    if op.status != "Failed":
        raise ValueError("BUG: can only format Operation with .status == Failed")
    message = None
    code = None
    operation_name = None
    operation_id = None

    try:
        operation_id = op.id
        error = op.error
        message = error.message
        code = error.pure_code
        if not code:
            code = error.http_code
        operation_name = op.request_type
    except Exception as e:
        pass

    output = ""
    if operation_name:
        # converts e.g. 'CreateVolume' to 'Create volume'
        operation_name = re.sub("(.)([A-Z][a-z]+)", r"\1 \2", operation_name)
        operation_name = re.sub(
            "([a-z0-9])([A-Z])", r"\1 \2", operation_name
        ).capitalize()
        output += "{0}: ".format(operation_name)
    output += "operation failed"

    if message:
        output += ", {0}".format(message.replace('"', "'"))

    parenthesed = False
    if code:
        if not parenthesed:
            output += " ("
            parenthesed = True
        output += "code: '{0}'".format(code)
    if operation_id:
        if not parenthesed:
            output += " ("
            parenthesed = True
        else:
            output += ", "
        output += "operation id: '{0}'".format(operation_id)
    if parenthesed:
        output += ")"

    return output


def _handle_api_exception(
    module,
    exception,
    traceback,
    verbose,
):
    (error_message, body) = format_fusion_api_exception(exception, traceback)

    if verbose:
        module.fail_json(msg=error_message, call_details=body)
    else:
        module.fail_json(msg=error_message)


def _handle_operation_exception(module, exception, verbose):
    op = exception.op

    error_message = format_failed_fusion_operation(op)

    if verbose:
        module.fail_json(msg=error_message, op_details=op.to_dict())
    else:
        module.fail_json(msg=error_message)


def _except_hook_callback(module, original_hook, type, value, traceback):
    verbose = _should_be_verbose(module)
    if type == purefusion.rest.ApiException:
        _handle_api_exception(
            module,
            value,
            traceback,
            verbose,
        )
    elif type == OperationException:
        _handle_operation_exception(module, value, verbose)
    # TODO handle urllib.Protocol here as it is fired by the API on HTTP errors
    # https://urllib3.readthedocs.io/en/stable/reference/urllib3.exceptions.html#urllib3.exceptions.ProtocolError

    # if we bubbled here the handlers were not able to process the exception
    original_hook(type, value, traceback)


def install_fusion_exception_hook(module):
    """Installs a hook that catches `purefusion.rest.ApiException` and
    `OperationException` and produces simpler and nicer error messages
    for Ansible output."""
    original_hook = sys.excepthook
    sys.excepthook = lambda type, value, traceback: _except_hook_callback(
        module, original_hook, type, value, traceback
    )