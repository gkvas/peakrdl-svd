import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, Optional, Tuple, Type

from systemrdl import rdltypes, RDLCompiler
from systemrdl.importer import RDLImporter


# ---------------------------------------------------------------------------
# SVD access-mode → SystemRDL access-type tables
# ---------------------------------------------------------------------------

_ACCESS: Dict[str, Any] = {
    "read-write":     rdltypes.AccessType.rw,
    "read-only":      rdltypes.AccessType.r,
    "write-only":     rdltypes.AccessType.w,
    # SVD "writeOnce" / "read-writeOnce" have no direct RDL equivalent;
    # rw is the safe approximation.
    "writeOnce":      rdltypes.AccessType.rw,
    "read-writeOnce": rdltypes.AccessType.rw,
}

_ONWRITE: Dict[str, Any] = {
    "oneToClear":   rdltypes.OnWriteType.woclr,
    "oneToSet":     rdltypes.OnWriteType.woset,
    "oneToToggle":  rdltypes.OnWriteType.wot,
    "zeroToClear":  rdltypes.OnWriteType.wzc,
    "zeroToSet":    rdltypes.OnWriteType.wzs,
    "zeroToToggle": rdltypes.OnWriteType.wzt,
    "clear":        rdltypes.OnWriteType.wclr,
    "set":          rdltypes.OnWriteType.wset,
}

_ONREAD: Dict[str, Any] = {
    "clear":          rdltypes.OnReadType.rclr,
    "set":            rdltypes.OnReadType.rset,
    "modifyExternal": rdltypes.OnReadType.ruser,
}


# ---------------------------------------------------------------------------
# Module-level XML helpers (stateless, no self needed)
# ---------------------------------------------------------------------------

def _text(el: ET.Element, tag: str) -> Optional[str]:
    child = el.find(tag)
    if child is not None and child.text:
        return child.text.strip()
    return None


def _int(el: ET.Element, tag: str) -> Optional[int]:
    t = _text(el, tag)
    return _parse_int(t) if t is not None else None


def _parse_int(s: str) -> int:
    s = s.strip()
    if s.lower().startswith("0x"):
        return int(s, 16)
    if s.startswith("#"):          # IP-XACT / some SVD variants
        return int(s[1:], 16)
    return int(s, 0)


def _sanitize(name: str) -> str:
    """
    Turn an SVD name into a valid SystemRDL identifier.
    Strips the '%s' array placeholder, replaces illegal chars with '_',
    prepends '_' if the name starts with a digit.
    """
    name = name.replace("%s", "")
    name = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    if name and name[0].isdigit():
        name = "_" + name
    name = name.strip("_")
    return name or "_unnamed"


# ---------------------------------------------------------------------------
# Importer
# ---------------------------------------------------------------------------

class SVDImporter(RDLImporter):
    """
    Imports a CMSIS-SVD file into the SystemRDL compiler model.

    Supported SVD features:
      - Peripheral derivedFrom (register layout reuse at a different base address)
      - Register / cluster derivedFrom (within the same peripheral)
      - Register and cluster arrays via <dim> / <dimIncrement>
      - Nested clusters (cluster containing clusters) → nested regfiles
      - All three field bit-range notations (bitOffset+bitWidth, lsb+msb, bitRange)
      - Device / peripheral / register / cluster level default inheritance
        (size, access, resetValue, resetMask)
      - <enumeratedValues> → UserEnum encoding
      - modifiedWriteValues and readAction field modifiers
    """

    def __init__(self, compiler: RDLCompiler, peripheral_filter: Optional[str] = None) -> None:
        super().__init__(compiler)
        self._peripheral_filter = peripheral_filter
        # Maps sanitized peripheral name → addrmap definition (for derivedFrom)
        self._periph_defs: Dict[str, Any] = {}
        # Maps "periph.reg" → reg definition (for register-level derivedFrom)
        self._reg_defs: Dict[str, Any] = {}
        # Maps "periph.cluster" → regfile definition (for cluster-level derivedFrom)
        self._cluster_defs: Dict[str, Any] = {}
        # Counter for globally-unique enum type names
        self._enum_counter = 0
        # Named enumeratedValues registry: keys are scoped at multiple levels
        # so derivedFrom can resolve regardless of how specific the reference is.
        self._named_enums: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def import_file(self, path: str) -> None:
        super().import_file(path)
        root = ET.parse(path).getroot()
        self._parse_device(root)

    # ------------------------------------------------------------------
    # Device → top-level addrmap
    # ------------------------------------------------------------------

    def _parse_device(self, dev: ET.Element) -> None:
        dev_name = _sanitize(_text(dev, "name") or "device")
        dev_defaults = self._collect_defaults(dev, {
            "size": 32,
            "access": rdltypes.AccessType.rw,
            "reset_value": None,
            "reset_mask": None,
        })

        if self._peripheral_filter:
            # Register a single peripheral as the root component so the
            # Renode exporter sees it directly as the top node.
            for el in dev.findall("./peripherals/peripheral"):
                raw = _text(el, "name") or ""
                if raw == self._peripheral_filter or _sanitize(raw) == self._peripheral_filter:
                    periph_def = self._build_peripheral_def(el, dev_defaults)
                    if periph_def is not None:
                        self.register_root_component(periph_def)
                    return
            self.msg.warning(
                f"SVD: peripheral '{self._peripheral_filter}' not found in {dev_name}",
                self.default_src_ref,
            )
            return

        # Full device: register the device addrmap containing all peripherals.
        top = self.create_addrmap_definition(dev_name)
        for el in dev.findall("./peripherals/peripheral"):
            inst = self._parse_peripheral(el, dev_defaults)
            if inst is not None:
                self.add_child(top, inst)
        self.register_root_component(top)

    # ------------------------------------------------------------------
    # Peripheral → addrmap instance (child of device)
    # ------------------------------------------------------------------

    def _parse_peripheral(self, el: ET.Element, dev_defaults: dict):
        name = _sanitize(_text(el, "name") or "periph")
        base_addr = _int(el, "baseAddress") or 0
        derived = el.get("derivedFrom")

        if derived:
            base_def = self._periph_defs.get(_sanitize(derived))
            if base_def is None:
                self.msg.warning(
                    f"SVD: peripheral '{name}' derivedFrom '{derived}' not yet defined — skipping",
                    self.default_src_ref,
                )
                return None
            return self.instantiate_addrmap(base_def, name, base_addr)

        periph_def = self._build_peripheral_def(el, dev_defaults)
        if periph_def is None:
            return None
        return self.instantiate_addrmap(periph_def, name, base_addr)

    def _build_peripheral_def(self, el: ET.Element, dev_defaults: dict):
        name = _sanitize(_text(el, "name") or "periph")
        p_defaults = self._collect_defaults(el, dev_defaults)
        periph_def = self.create_addrmap_definition(name)

        for child in el.findall("./registers/*"):
            if child.tag == "register":
                for inst in self._parse_register(child, name, p_defaults):
                    self.add_child(periph_def, inst)
            elif child.tag == "cluster":
                for inst in self._parse_cluster(child, name, p_defaults):
                    self.add_child(periph_def, inst)

        self._periph_defs[name] = periph_def
        return periph_def

    # ------------------------------------------------------------------
    # Register → reg instance(s) (child of peripheral addrmap)
    # ------------------------------------------------------------------

    def _parse_register(self, el: ET.Element, periph_name: str, p_defaults: dict) -> list:
        name = _sanitize(_text(el, "name") or "reg")
        addr_offset = _int(el, "addressOffset") or 0
        dim = _int(el, "dim")
        dim_inc = _int(el, "dimIncrement")
        r_defaults = self._collect_defaults(el, p_defaults)

        derived = el.get("derivedFrom")
        if derived:
            base_def = self._reg_defs.get(f"{periph_name}.{_sanitize(derived)}")
            if base_def is None:
                self.msg.warning(
                    f"SVD: register '{name}' derivedFrom '{derived}' not yet defined — skipping",
                    self.default_src_ref,
                )
                return []
            return self._instantiate_reg(base_def, name, addr_offset, dim, dim_inc)

        reg_def = self.create_reg_definition(name)

        reg_size = r_defaults["size"]
        if reg_size != 32:
            self.assign_property(reg_def, "regwidth", reg_size)

        for field_el in el.findall("./fields/field"):
            f_inst = self._parse_field(field_el, periph_name, name, r_defaults)
            if f_inst is not None:
                self.add_child(reg_def, f_inst)

        self._reg_defs[f"{periph_name}.{name}"] = reg_def
        return self._instantiate_reg(reg_def, name, addr_offset, dim, dim_inc)

    def _instantiate_reg(self, reg_def, name, addr_offset, dim, dim_inc) -> list:
        if dim and dim_inc:
            return [
                self.instantiate_reg(reg_def, f"{name}{i}", addr_offset + i * dim_inc)
                for i in range(dim)
            ]
        return [self.instantiate_reg(reg_def, name, addr_offset)]

    # ------------------------------------------------------------------
    # Cluster → regfile instance(s) (child of peripheral addrmap or regfile)
    # ------------------------------------------------------------------

    def _parse_cluster(self, el: ET.Element, parent_name: str, p_defaults: dict) -> list:
        name = _sanitize(_text(el, "name") or "cluster")
        addr_offset = _int(el, "addressOffset") or 0
        dim = _int(el, "dim")
        dim_inc = _int(el, "dimIncrement")
        c_defaults = self._collect_defaults(el, p_defaults)
        qualified = f"{parent_name}.{name}"

        derived = el.get("derivedFrom")
        if derived:
            base_def = self._cluster_defs.get(f"{parent_name}.{_sanitize(derived)}")
            if base_def is None:
                self.msg.warning(
                    f"SVD: cluster '{name}' derivedFrom '{derived}' not yet defined — skipping",
                    self.default_src_ref,
                )
                return []
            return self._instantiate_regfile(base_def, name, addr_offset, dim, dim_inc)

        rf_def = self.create_regfile_definition(name)

        for child in el:
            if child.tag == "register":
                for inst in self._parse_register(child, qualified, c_defaults):
                    self.add_child(rf_def, inst)
            elif child.tag == "cluster":
                for inst in self._parse_cluster(child, qualified, c_defaults):
                    self.add_child(rf_def, inst)

        self._cluster_defs[qualified] = rf_def
        return self._instantiate_regfile(rf_def, name, addr_offset, dim, dim_inc)

    def _instantiate_regfile(self, rf_def, name, addr_offset, dim, dim_inc) -> list:
        if dim and dim_inc:
            return [
                self.instantiate_regfile(rf_def, f"{name}{i}", addr_offset + i * dim_inc)
                for i in range(dim)
            ]
        return [self.instantiate_regfile(rf_def, name, addr_offset)]

    # ------------------------------------------------------------------
    # Field → field instance (child of reg)
    # ------------------------------------------------------------------

    def _parse_field(self, el: ET.Element, periph_name: str, reg_name: str, r_defaults: dict):
        name = _sanitize(_text(el, "name") or "field")
        lsb, width = self._bit_range(el)
        if width <= 0:
            return None

        field_def = self.create_field_definition(name)

        # Access modes
        acc = _text(el, "access")
        sw = _ACCESS.get(acc, r_defaults["access"]) if acc else r_defaults["access"]
        self.assign_property(field_def, "sw", sw)
        self.assign_property(field_def, "hw", rdltypes.AccessType.r)

        # Reset value: extract the field's bits from the register reset value,
        # only if the reset mask confirms those bits have a defined reset state.
        reset_val = r_defaults.get("reset_value")
        reset_mask = r_defaults.get("reset_mask")
        if reset_val is not None:
            field_mask = ((1 << width) - 1) << lsb
            if reset_mask is None or (reset_mask & field_mask) == field_mask:
                field_reset = (reset_val >> lsb) & ((1 << width) - 1)
                self.assign_property(field_def, "reset", field_reset)

        # modifiedWriteValues → onwrite
        mwv = _text(el, "modifiedWriteValues")
        if mwv and mwv in _ONWRITE:
            self.assign_property(field_def, "onwrite", _ONWRITE[mwv])

        # readAction → onread
        ra = _text(el, "readAction")
        if ra and ra in _ONREAD:
            self.assign_property(field_def, "onread", _ONREAD[ra])

        # Enum encoding
        ev_el = el.find("enumeratedValues")
        if ev_el is not None:
            enum_type = self._parse_enum(ev_el, periph_name, reg_name, name)
            if enum_type is not None:
                self.assign_property(field_def, "encode", enum_type)

        # Description
        desc = _text(el, "description")
        if desc:
            self.assign_property(field_def, "desc", desc)

        return self.instantiate_field(field_def, name, lsb, width)

    # ------------------------------------------------------------------
    # EnumeratedValues → UserEnum
    # ------------------------------------------------------------------

    def _parse_enum(
        self,
        el: ET.Element,
        periph: str,
        reg: str,
        field: str,
    ) -> Optional[Type[rdltypes.UserEnum]]:
        derived = el.get("derivedFrom")
        if derived:
            resolved = self._resolve_named_enum(derived, periph, reg, field)
            if resolved is None:
                self.msg.warning(
                    f"SVD: enumeratedValues derivedFrom='{derived}' in "
                    f"{periph}.{reg}.{field} could not be resolved — enum encoding skipped",
                    self.default_src_ref,
                )
            return resolved

        members = []
        seen_values: set = set()
        seen_names: set = set()

        for ev in el.findall("enumeratedValue"):
            ev_name_raw = _text(ev, "name") or ""
            ev_val_str = _text(ev, "value")
            if not ev_name_raw or ev_val_str is None:
                continue
            try:
                ev_val = _parse_int(ev_val_str)
            except ValueError:
                # SVD allows the special token "default" for catch-all; skip it.
                continue

            ev_name = _sanitize(ev_name_raw)
            if ev_val in seen_values or ev_name in seen_names:
                continue
            seen_values.add(ev_val)
            seen_names.add(ev_name)

            desc = _text(ev, "description")
            members.append(rdltypes.UserEnumMemberContainer(ev_name, ev_val, None, desc))

        if not members:
            return None

        self._enum_counter += 1
        type_name = f"{periph}_{reg}_{field}_e{self._enum_counter}"
        try:
            enum_type = rdltypes.UserEnum.define_new(type_name, members)
        except Exception:
            return None

        # If the <enumeratedValues> element carries a <name> child, register it
        # so that other fields can reference it via derivedFrom.
        ev_type_name = _text(el, "name")
        if ev_type_name:
            self._register_named_enum(ev_type_name, periph, reg, field, enum_type)

        return enum_type

    def _register_named_enum(
        self,
        ev_name: str,
        periph: str,
        reg: str,
        field: str,
        enum_type: Type[rdltypes.UserEnum],
    ) -> None:
        # Store under progressively qualified keys.  setdefault means the first
        # (most specific) definition wins if the same short name appears twice.
        for key in (
            f"{periph}.{reg}.{field}.{ev_name}",  # fully qualified
            f"{periph}.{reg}.{ev_name}",           # register scope
            f"{periph}.{ev_name}",                 # peripheral scope
            ev_name,                               # bare name
        ):
            self._named_enums.setdefault(key, enum_type)

    def _resolve_named_enum(
        self,
        ref: str,
        periph: str,
        reg: str,
        field: str,
    ) -> Optional[Type[rdltypes.UserEnum]]:
        # Try the reference verbatim first (handles a fully-qualified path if
        # the SVD author used one), then progressively prepend context so a
        # bare name resolves from innermost scope outward.
        for key in (
            ref,
            f"{field}.{ref}",
            f"{reg}.{ref}",
            f"{reg}.{field}.{ref}",
            f"{periph}.{ref}",
            f"{periph}.{reg}.{ref}",
            f"{periph}.{reg}.{field}.{ref}",
        ):
            if key in self._named_enums:
                return self._named_enums[key]
        return None

    # ------------------------------------------------------------------
    # Bit-range parsing — SVD supports three equivalent notations
    # ------------------------------------------------------------------

    @staticmethod
    def _bit_range(el: ET.Element) -> Tuple[int, int]:
        # Notation 1: <bitOffset> + <bitWidth>
        bo = _int(el, "bitOffset")
        bw = _int(el, "bitWidth")
        if bo is not None and bw is not None:
            return bo, bw

        # Notation 2: <lsb> + <msb>
        lsb = _int(el, "lsb")
        msb = _int(el, "msb")
        if lsb is not None and msb is not None:
            return lsb, msb - lsb + 1

        # Notation 3: <bitRange>[msb:lsb]</bitRange>
        br = _text(el, "bitRange")
        if br:
            m = re.match(r"\[(\d+):(\d+)\]", br.strip())
            if m:
                msb_v, lsb_v = int(m.group(1)), int(m.group(2))
                return lsb_v, msb_v - lsb_v + 1

        return 0, 0

    # ------------------------------------------------------------------
    # Default inheritance: device → peripheral → cluster → register
    # ------------------------------------------------------------------

    @staticmethod
    def _collect_defaults(el: ET.Element, parent: dict) -> dict:
        d = dict(parent)
        size = _int(el, "size")
        if size is not None:
            d["size"] = size
        acc = _text(el, "access")
        if acc and acc in _ACCESS:
            d["access"] = _ACCESS[acc]
        rv = _int(el, "resetValue")
        if rv is not None:
            d["reset_value"] = rv
        rm = _int(el, "resetMask")
        if rm is not None:
            d["reset_mask"] = rm
        return d
