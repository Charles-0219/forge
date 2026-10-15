from __future__ import annotations


class ForgeError(Exception):
    code = 500


class Conflict(ForgeError):
    code = 409


class NotFound(ForgeError):
    code = 404


class IncompletePairing(ForgeError):
    code = 409


class DuplicateTerminal(ForgeError):
    code = 409


class ProtocolMismatch(ForgeError):
    code = 409


class Gone(ForgeError):
    code = 410
