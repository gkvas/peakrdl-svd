"""
Tests for <enumeratedValues derivedFrom="..."> resolution.

Covers:
  - bare-name reference to a sibling <enumeratedValues> in the same field
  - bare-name reference across fields in the same register
  - unresolvable reference (should warn and leave encode unset, not crash)
"""

import os
import tempfile
import textwrap

import pytest
from systemrdl import RDLCompiler

from peakrdl_svd.importer import SVDImporter


SVD = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <device>
      <name>TEST</name>
      <addressUnitBits>8</addressUnitBits>
      <width>32</width>
      <peripherals>
        <peripheral>
          <name>PERIPH</name>
          <baseAddress>0x40000000</baseAddress>
          <registers>
            <register>
              <name>CTRL</name>
              <addressOffset>0x0</addressOffset>
              <fields>
                <field>
                  <name>MODE</name>
                  <bitOffset>0</bitOffset>
                  <bitWidth>2</bitWidth>
                  <enumeratedValues>
                    <name>ModeValues</name>
                    <enumeratedValue><name>Off</name><value>0</value></enumeratedValue>
                    <enumeratedValue><name>On</name><value>1</value></enumeratedValue>
                    <enumeratedValue><name>Auto</name><value>2</value></enumeratedValue>
                  </enumeratedValues>
                  <enumeratedValues derivedFrom="ModeValues">
                    <usage>write</usage>
                  </enumeratedValues>
                </field>
                <field>
                  <name>ALT_MODE</name>
                  <bitOffset>4</bitOffset>
                  <bitWidth>2</bitWidth>
                  <enumeratedValues derivedFrom="ModeValues"/>
                </field>
                <field>
                  <name>BROKEN</name>
                  <bitOffset>8</bitOffset>
                  <bitWidth>2</bitWidth>
                  <enumeratedValues derivedFrom="NoSuchEnum"/>
                </field>
              </fields>
            </register>
          </registers>
        </peripheral>
      </peripherals>
    </device>
""")

MODE_MEMBERS = {"Off": 0, "On": 1, "Auto": 2}


@pytest.fixture
def svd_file(tmp_path):
    p = tmp_path / "test.svd"
    p.write_text(SVD)
    return str(p)


@pytest.fixture
def elaborated(svd_file):
    rdlc = RDLCompiler()
    SVDImporter(rdlc, peripheral_filter="PERIPH").import_file(svd_file)
    root = rdlc.elaborate()
    periph = root.children()[0]
    reg = periph.children()[0]
    return {n.inst_name: n for n in reg.children()}


def test_own_enum_defined(elaborated):
    enc = elaborated["MODE"].get_property("encode")
    assert enc is not None
    assert {m.name: m.value for m in enc} == MODE_MEMBERS


def test_cross_field_resolution(elaborated):
    enc = elaborated["ALT_MODE"].get_property("encode")
    assert enc is not None
    assert {m.name: m.value for m in enc} == MODE_MEMBERS


def test_unresolvable_warns_and_leaves_unset(svd_file):
    rdlc = RDLCompiler()
    warnings = []
    rdlc.env.msg.warning = lambda msg, *a, **kw: warnings.append(str(msg))

    SVDImporter(rdlc, peripheral_filter="PERIPH").import_file(svd_file)
    root = rdlc.elaborate()
    reg = root.children()[0].children()[0]
    fields = {n.inst_name: n for n in reg.children()}

    assert fields["BROKEN"].get_property("encode") is None
    assert any("NoSuchEnum" in w for w in warnings)
