"""Schema-safe IPC identifiers with an explicit, reversible sidecar mapping."""
import hashlib
import re

# Use the intersection of IPC shortName and qualifiedNameType. Other attributes
# (e.g. part, material, text, descriptions) retain their original text.
SAFE = re.compile(r"^[A-Za-z0-9_.+<>-]+$")
IDENTIFIERS = {"name", "id", "layerRef", "layerOrGroupRef", "fromLayer", "toLayer",
               "net", "netRef", "refDes", "componentRef", "packageRef", "padstackDefRef",
               "pin", "pinOne", "number", "stepRef", "roleRef", "enterpriseRef", "standardPrimitiveRef"}


class IPCWriter:
    def __init__(self, target, report):
        self.target, self.report = target, report
        self.mapping, self.reverse = {}, {}

    def name(self, value):
        if SAFE.fullmatch(value) and not value.startswith("X_"):
            return value
        if value not in self.mapping:
            encoded = "X_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
            if encoded in self.reverse and self.reverse[encoded] != value:
                raise ValueError("Identifier digest collision; conversion stopped")
            self.mapping[value] = encoded
            self.reverse[encoded] = value
            self.report.warn("IPC_IDENTIFIER_ENCODED", f"{value} -> {encoded}; full mapping is in identifier_mapping")
        return self.mapping[value]

    def attrs(self, attrs):
        return {k: self.name(str(v)) if k in IDENTIFIERS else str(v) for k, v in attrs.items()}

    def write(self, *nodes):
        for node in nodes:
            for child in node.iter():
                if isinstance(child.tag, str):
                    child.attrib.update(self.attrs(child.attrib))
            self.target.write(node)

    def element(self, tag, attrs=None, nsmap=None, **kwargs):
        return self.target.element(tag, self.attrs({**(attrs or {}), **kwargs}), nsmap=nsmap)

    def write_declaration(self):
        self.target.write_declaration()
