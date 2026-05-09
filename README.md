# peakrdl-svd

A [PeakRDL](https://github.com/SystemRDL/PeakRDL) importer plugin that reads
[CMSIS-SVD](https://open-cmsis-pack.github.io/svd-spec/main/index.html) (System
View Description) register description files and compiles them into the
SystemRDL register model.

## Installation

```bash
pip install peakrdl-svd
```

Once installed, the plugin registers itself automatically under the `svd`
importer name, so PeakRDL picks it up with no extra configuration.

## Usage

### CLI

Import an SVD file and export to another format (e.g. IP-XACT):

```bash
peakrdl ipxact path/to/device.svd -o out/
```

Import only a single peripheral:

```bash
peakrdl ipxact path/to/device.svd --svd-peripheral UART0 -o out/
```

### Python API

```python
from systemrdl import RDLCompiler
from peakrdl_svd.importer import SVDImporter

rdlc = RDLCompiler()
imp = SVDImporter(rdlc, peripheral_filter="UART0")  # omit filter for full device
imp.import_file("path/to/device.svd")

root = rdlc.elaborate()
for periph in root.children():
    print(periph.inst_name)
```

## Features

- **Full device or single-peripheral import** via `--svd-peripheral`
- **`derivedFrom`** — peripherals, registers, clusters, and `<enumeratedValues>`
- **Register and cluster arrays** via `<dim>` / `<dimIncrement>`
- **Nested clusters** mapped to nested `regfile` components
- **All three SVD bit-range notations**: `bitOffset+bitWidth`, `lsb+msb`, `[msb:lsb]`
- **Default inheritance** — `size`, `access`, `resetValue`, `resetMask` propagate
  from device → peripheral → cluster → register
- **`<enumeratedValues>`** mapped to `UserEnum` encoding properties
- **`modifiedWriteValues`** and **`readAction`** mapped to `onwrite` / `onread`

## Development

```bash
git clone https://github.com/gkvas/peakrdl-svd
cd peakrdl-svd
pip install -e ".[dev]"
pytest
```

## License

MIT — see [LICENSE](LICENSE).
