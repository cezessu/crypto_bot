"""Minimal requests fallback for isolated unit tests without installed deps."""

import sys
import types


def install_requests_stub_if_missing():
    try:
        __import__("requests")
        return
    except ModuleNotFoundError:
        pass

    module = types.ModuleType("requests")

    class RequestException(Exception):
        pass

    class Timeout(RequestException):
        pass

    class Session:
        def get(self, *args, **kwargs):
            raise AssertionError("Unit tests must inject a fake MEXC HTTP session")

    module.RequestException = RequestException
    module.Timeout = Timeout
    module.Session = Session
    sys.modules["requests"] = module
