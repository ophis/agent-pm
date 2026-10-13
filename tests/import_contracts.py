"""Import Linter contracts for nested repository units; literal imports use the same policy."""

import sys

from importlinter import Contract, ContractCheck, fields, output


def _within(module, package):
    return module == package or module.startswith(package + ".")


def _scope(module, units):
    return max((unit for unit in units if _within(module, unit)), key=len, default="outside")


def violation(importer, imported, policy):
    if imported.split(".")[0] in sys.stdlib_module_names:
        return False
    container, units = policy["container"], policy["units"]
    if importer.startswith("agent_pm.") and any(
        _within(imported, unit) for unit in policy.get("unimported_units", ())
    ):
        return True
    if not _within(importer, container):
        return False
    own = _scope(importer, units)
    if _within(imported, container) and _scope(imported, units) == own:
        return False
    permitted = []
    for declaration in policy["dependencies"]:
        source, _, targets = declaration.partition(" -> ")
        if source == own or source != container and _within(importer, source):
            permitted.extend(targets.split())
    return not any(
        _scope(imported, units) == target
        if _within(target, container)
        else _within(imported, target)
        for target in permitted
    )


class UnitsContract(Contract):
    container = fields.StringField()
    units = fields.ListField(subfield=fields.StringField())
    dependencies = fields.ListField(subfield=fields.StringField())
    unimported_units = fields.ListField(subfield=fields.StringField(), default=[])

    def check(self, graph, verbose):
        broken = []
        for importer in sorted(graph.modules):
            for imported in sorted(graph.find_modules_directly_imported_by(importer)):
                if violation(importer, imported, self.contract_options):
                    broken.extend(graph.get_import_details(importer=importer, imported=imported))
        return ContractCheck(kept=not broken, metadata={"broken": broken})

    def render_broken_contract(self, check):
        for edge in check.metadata["broken"]:
            output.print_error(
                f"{edge['importer']}:{edge['line_number']}: imports {edge['imported']}"
            )
