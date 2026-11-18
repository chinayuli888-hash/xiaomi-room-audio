# Third-party software and attribution

New project work and the combined source project use **GPL-3.0-only**.
Upstream portions retain their original notices and permissions. See
[LICENSE](LICENSE), [NOTICE](NOTICE) and the pinned [provenance review](docs/PROVENANCE.md).

| Component or reference | Use | License / retained notice |
| --- | --- | --- |
| [pyatv](https://github.com/postlund/pyatv) | Adapted TLV8, timestamps, encrypted framing, pairing and transport behavior | [MIT](licenses/pyatv-MIT.txt) |
| [airplay2-sender-cpp](https://github.com/akustikrausch/airplay2-sender-cpp) | ALAC packing, AP2 setup/event ordering and minimum-response behavior | [Apache-2.0](licenses/Apache-2.0.txt), [NOTICE](licenses/airplay2-sender-cpp-NOTICE.txt), [upstream third-party notices](licenses/airplay2-sender-cpp-THIRD-PARTY-NOTICES.txt) |
| [srptools 2.0.0](https://pypi.org/project/srptools/2.0.0/) | Unmodified vendored SRP package | [BSD-3-Clause](srptools/LICENSE) |
| [OwnTone](https://github.com/owntone/owntone-server) | Historical protocol reference; possible adaptations treated under its GPLv3 option | Relevant output files: GPL-2.0-or-later; [headers](licenses/OwnTone-headers.txt), [GPLv2 text](licenses/GPL-2.0-OwnTone.txt) |
| [saundo](https://github.com/4202217/saundo) | Historical binary interoperability reference | GPL-3.0-only project; some core files Apache-2.0. [Upstream NOTICE](licenses/saundo-NOTICE.txt). No Go source, executable or artwork bundled |
| [Shairport Sync](https://github.com/mikebrady/shairport-sync) | ALAC wire-format reference | Referenced decoder's [MIT notice](licenses/Shairport-alac-MIT.txt) |
| [libraop / RAOP-Player](https://github.com/philippe44/libraop) | Credited by pyatv's timestamp helpers | [Original statement](licenses/RAOP-Player-MIT.txt); no C library bundled |
| [AudioDeviceCmdlets](https://github.com/frgnca/AudioDeviceCmdlets) | PolicyConfig interface signature reference | [MIT](licenses/AudioDeviceCmdlets-MIT.txt); no C# implementation bundled |

The copied upstream NOTICE files describe their original projects and can name
components absent here. This repository does not include upstream executables,
SF Symbols, fonts, Mbed TLS, Ed25519 C code, or AirFlash capture/PTP code.

## Installed separately

- [python-zeroconf / zeroconf](https://pypi.org/project/zeroconf/): **LGPL-2.1-or-later**,
  used for local IPv4 DNS-SD discovery. Installed via pip from `requirements.txt`;
  its source and binaries are not vendored in this repository. The upstream
  [COPYING](https://github.com/python-zeroconf/python-zeroconf/blob/master/COPYING)
  and distribution notices apply to that separately installed package.
- [NumPy](https://numpy.org/): BSD-3-Clause, with its distribution's own notices.
- [cryptography](https://cryptography.io/): Apache-2.0 OR BSD-3-Clause, with its
  dependency notices.
- Python and Tk are supplied by the user's installation and keep their own
  PSF/Tcl/Tk notices.
- [FFmpeg](https://ffmpeg.org/legal.html) runs as a separate process. Its LGPL/GPL
  terms depend on the chosen build; no FFmpeg binary is included.
- [VB-Audio Virtual Cable](https://vb-audio.com/Cable/) is a separately obtained
  driver, subject to its publisher's terms; it is not included.

This is an independent compatibility project, not an official Xiaomi or Apple
product. Product names identify device/protocol compatibility and grant no
trademark rights.
