from typing import TYPE_CHECKING

from peakrdl.plugins.importer import ImporterPlugin as ImporterBase

from .importer import SVDImporter

if TYPE_CHECKING:
    import argparse
    from systemrdl import RDLCompiler


class Importer(ImporterBase):
    name = "svd"
    file_extensions = ["xml", "svd"]

    # peakrdl-renode always nests the -N value inside Antmicro.Renode.Peripherals.
    # Strip that prefix here if the user passed the full intuitive path, so the
    # generated namespace is not doubled.
    _RENODE_NS_PREFIX = "Antmicro.Renode.Peripherals."

    def is_compatible(self, path: str) -> bool:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                head = f.read(4096)
            return "<device" in head and "<peripherals>" in head
        except OSError:
            return False

    def add_importer_arguments(self, arg_group: "argparse._ActionsContainer") -> None:
        arg_group.add_argument(
            "--svd-peripheral",
            metavar="NAME",
            default=None,
            help=(
                "Import only this peripheral from the SVD (case-sensitive). "
                "If omitted all peripherals are imported under the device addrmap."
            ),
        )

    def do_import(self, rdlc: "RDLCompiler", options: "argparse.Namespace", path: str) -> None:
        ns = getattr(options, "namespace", None)
        if ns and ns.startswith(self._RENODE_NS_PREFIX):
            options.namespace = ns[len(self._RENODE_NS_PREFIX):]

        peripheral_filter = getattr(options, "svd_peripheral", None)
        imp = SVDImporter(rdlc, peripheral_filter=peripheral_filter)
        imp.import_file(path)
