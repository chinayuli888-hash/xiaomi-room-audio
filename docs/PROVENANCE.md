# Source provenance and license review

Reviewed 2026-11-18 for the v0.6 source release.

**Decision: new project work and the combined source use GPL-3.0-only, with
upstream licenses and notices preserved.** Examined MIT, Apache-2.0 and
BSD-3-Clause adaptations are retained under their original terms. Any
OwnTone-derived portion uses its GPLv3 option; any saundo-derived portion is
compatible with the combined GPLv3-only work. This does not relicense unmodified
upstream packages or grant rights to drivers, binaries, artwork or trademarks.

The formerly unidentified C++ reference is
`akustikrausch/airplay2-sender-cpp`. No unattributed C++ source or binary needs
to be included to run this Python implementation.

## Evidence and pinned versions

Original development artifacts contain a protocol design note, the original
Python sender, downloaded reference archives, and the srptools wheel.
The design note names pyatv and airplay2-sender-cpp as the implementations
consulted when the sender was created. GitHub archive PAX `comment` fields
record the commits used; they were checked against upstream. The private
design note's SHA-256 is
`325b557f8f66a02772d5c831b866058221d84eab9b8ad9b6543c9838c1587941`.
Private machine paths, device addresses, keys and recordings are excluded.

| Upstream | Pinned source | License evidence |
| --- | --- | --- |
| pyatv | [b277a4c8222ecdcbaab8a24e3e713ca44765adb4](https://github.com/postlund/pyatv/tree/b277a4c8222ecdcbaab8a24e3e713ca44765adb4) | [MIT](https://github.com/postlund/pyatv/blob/b277a4c8222ecdcbaab8a24e3e713ca44765adb4/LICENSE.md) |
| airplay2-sender-cpp | [fc913509039412790aa2427558d35178aa7842ea](https://github.com/akustikrausch/airplay2-sender-cpp/tree/fc913509039412790aa2427558d35178aa7842ea) | [Apache-2.0](https://github.com/akustikrausch/airplay2-sender-cpp/blob/fc913509039412790aa2427558d35178aa7842ea/LICENSE), [NOTICE](https://github.com/akustikrausch/airplay2-sender-cpp/blob/fc913509039412790aa2427558d35178aa7842ea/NOTICE) |
| OwnTone | [8ed84f2b3cba25d3de65e05660d59ce034b5adab](https://github.com/owntone/owntone-server/tree/8ed84f2b3cba25d3de65e05660d59ce034b5adab) | [airplay.c](https://github.com/owntone/owntone-server/blob/8ed84f2b3cba25d3de65e05660d59ce034b5adab/src/outputs/airplay.c) / [raop.c](https://github.com/owntone/owntone-server/blob/8ed84f2b3cba25d3de65e05660d59ce034b5adab/src/outputs/raop.c) headers: GPL-2.0-or-later |
| srptools | [2.0.0 wheel](https://pypi.org/project/srptools/2.0.0/); [source a68bf1e54974eab767c42fb112e823158446affa](https://github.com/idlesign/srptools/tree/a68bf1e54974eab767c42fb112e823158446affa) | Wheel metadata / `srptools/LICENSE`: BSD-3-Clause |
| saundo | [01729d5758f6828de6530030f0b7a6c87ac6d1a3](https://github.com/4202217/saundo/tree/01729d5758f6828de6530030f0b7a6c87ac6d1a3) | [README](https://github.com/4202217/saundo/blob/01729d5758f6828de6530030f0b7a6c87ac6d1a3/README.md): GPL-3.0-only; some core files have Apache-2.0 headers |
| Shairport Sync | [7ce90d89529dd760488f20592a7cbff626672f6c](https://github.com/mikebrady/shairport-sync/tree/7ce90d89529dd760488f20592a7cbff626672f6c) | Referenced [alac.c](https://github.com/mikebrady/shairport-sync/blob/7ce90d89529dd760488f20592a7cbff626672f6c/alac.c) carries David Hammerton's MIT notice |
| libraop / RAOP-Player | [c14088da44df37f58ef7a45b8ce8a3cbc051c5e4](https://github.com/philippe44/libraop/tree/c14088da44df37f58ef7a45b8ce8a3cbc051c5e4) | [Original license statement](https://github.com/philippe44/libraop/blob/c14088da44df37f58ef7a45b8ce8a3cbc051c5e4/LICENSE.txt) |
| AudioDeviceCmdlets | [26d08d30e9d804c016c994117f9b038b7d50da95](https://github.com/frgnca/AudioDeviceCmdlets/tree/26d08d30e9d804c016c994117f9b038b7d50da95) | [MIT](https://github.com/frgnca/AudioDeviceCmdlets/blob/26d08d30e9d804c016c994117f9b038b7d50da95/LICENSE) |

Historical archive SHA-256 values:

| Archive | SHA-256 |
| --- | --- |
| pyatv reference archive | `63aafbbfecebc0449f45986b0e832f8d7fccac291ac913d8e66cc51530257880` |
| C++ sender reference archive | `32ca8bc040f481579a98802142639c2de8fd2d03c441a1aa4c5d4135191c9d9d` |
| OwnTone reference archive | `3eb58f17c50ad754d2d6c7644feb940dbe686a2355dfcb73f34e4cece4b4d57e` |
| `srptools-2.0.0-py3-none-any.whl` | `566b1cc55964996296b6389294c61f7ee8072f12de800bff4be625f79d930c9b` |

The wheel digest matches [official PyPI metadata](https://pypi.org/pypi/srptools/2.0.0/json).

## File-level findings

| Project file | Finding and treatment |
| --- | --- |
| `ap2/tlv8.py` | Declared pyatv port. Tags, transient flag and reader/writer behavior compared against [hap_tlv8.py](https://github.com/postlund/pyatv/blob/b277a4c8222ecdcbaab8a24e3e713ca44765adb4/pyatv/auth/hap_tlv8.py); MIT attribution retained. |
| `ap2/audio.py` | Timestamp conversions match [pyatv timing.py](https://github.com/postlund/pyatv/blob/b277a4c8222ecdcbaab8a24e3e713ca44765adb4/pyatv/protocols/raop/timing.py). Slow ALAC packing follows `encodeAlacFrame_` in pinned [raop_sender.cpp](https://github.com/akustikrausch/airplay2-sender-cpp/blob/fc913509039412790aa2427558d35178aa7842ea/src/raop_sender.cpp); fast packing preserves that layout. MIT and Apache notices retained. Shairport is a format reference, not a bundled decoder. |
| `ap2/chacha.py` | Declares pyatv framing behavior; compared with `support/chacha20.py` and `auth/hap_session.py`. cryptography implements the actual cipher. MIT notice retained. |
| `ap2/hap_srp.py` | Transient pairing follows the design note and pyatv's `auth/hap_srp.py`; SRP arithmetic delegates to srptools. MIT and BSD notices retained. |
| `ap2/session.py` | Setup/event ordering and the unusual minimal 200 reply match the design note and pinned C++ recipe. Comments additionally cite OwnTone and saundo. The GPLv3 combined treatment conservatively covers possible adaptations; MIT/Apache and relevant reference notices retained. Release changes also reject missing or invalid receiver proofs. |
| `ap2/rtsp.py` | Blocking Python socket/RTSP parser with pyatv/reference transport behavior; preserve pyatv credit rather than claiming unrelated provenance. |
| `ap2/group.py` | Project group/timeline/per-speaker layer using inherited session/timing helpers. New work GPL-3.0-only; helper notices retained. |
| `srptools/*.py` | Nine files plus LICENSE byte-identical to the original wheel. Pinned Git source matches after newline normalization. No edits or relicensing. |
| `windows_audio.py` | Independent ctypes bindings; PolicyConfig GUID and signature checked against [IPolicyConfig.cs](https://github.com/frgnca/AudioDeviceCmdlets/blob/26d08d30e9d804c016c994117f9b038b7d50da95/SOURCE/IPolicyConfig.cs). No C# implementation shipped; MIT notice retained. |
| UI, routing/mixer, launcher, tests, icon and project docs | New project work under GPL-3.0-only. Icon/overview are project-created, not imported Xiaomi/Apple branding. |

The eight original inherited `ap2` files were retained for comparison. Six
were unchanged on import; `group.py` and `session.py` had project changes by
v0.6. These baseline hashes identify the originals **before** release notices,
not the final release manifest:

| Original file | SHA-256 |
| --- | --- |
| `__init__.py` | `a97673e297d89e959119fc089c127a0532b7cdf0c68bf1671936f4d1076d40bb` |
| `audio.py` | `5a8b12f87a1f3c2c633cb1c6c6f238bb43d0871df0af0f5b0d4e3893db035994` |
| `chacha.py` | `f1c1bc6b7de9fb91e7a3054b520c0c7a10442f920085315992692ac2867a7672` |
| `group.py` | `673ed6de33ee4ed80cc8a2bc8fe1af142d9b0062a6447d9f35f48bed05906018` |
| `hap_srp.py` | `f4a47977b49e2f7fe0be93ab0191af2e859cca0c32cda87a57245c28f8c27605` |
| `rtsp.py` | `299f08ac89e57d853150d0a309b52930c7358b217b07b0b4c55aea5a1449680a` |
| `session.py` | `84756cde1af9fd30a4fb427f3395b4833c9b93c48abf5f5870595721df5868b9` |
| `tlv8.py` | `f25df0406bc5a52c0911c0131de9b1207e5cc0236ed74d317a0b1fa540e2a1df` |

## License decision and limits

GPL-3.0-only avoids claiming the sender is entirely original or entirely
permissive. OwnTone's relevant headers permit GPLv2 **or later**, enabling
the GPLv3 option; saundo specifies version 3 only. MIT, Apache-2.0 and
BSD-3-Clause notices remain with their original portions. This follows
[GNU compatibility guidance](https://www.gnu.org/licenses/gpl-faq.en.html#v2v3Compatibility)
and [Apache's guidance](https://www.apache.org/licenses/GPL-compatibility.html).

File headers identify modifications and Python adaptations. NOTICE preserves
authors; required license texts and upstream NOTICE files accompany the source.
This review does not claim every protocol field was copied from OwnTone or
saundo merely because they are mentioned. The historical saundo reference was
a binary probe, not vendored Go code. The chosen license avoids relying on a
claim that every inherited expression was independently created.

The source tree includes no unresolved incompatible component identified in
this review. FFmpeg, the VB-CABLE driver, Python/Tk, NumPy and cryptography are
installed separately. No firmware, Apple certificate, persistent device key,
recording, saundo/AudioSync binary, SF Symbol or bundled font is distributed.
Preserved upstream NOTICE files retain their original context and can list
components absent here.

This is a source-distribution review, not a patent/trademark clearance or a
legal opinion about all possible prior art. Future executables, assets and
dependencies need their own distribution checks.
