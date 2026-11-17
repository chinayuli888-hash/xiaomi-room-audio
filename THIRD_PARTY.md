# Third-party components and upstream references

- The `ap2/` modules come from the AirPlay sender inherited by this project. The TLV code and timestamp helpers contain adaptations of [pyatv](https://github.com/postlund/pyatv) ([MIT notice](licenses/pyatv-MIT.txt)). pyatv's [RAOP timing module](https://github.com/postlund/pyatv/blob/master/pyatv/protocols/raop/timing.py) also credits [libraop / RAOP-Player](https://github.com/philippe44/libraop) ([MIT notice](licenses/RAOP-Player-MIT.txt)).
- Other protocol notes cite [Shairport Sync](https://github.com/mikebrady/shairport-sync), [OwnTone](https://github.com/owntone/owntone-server) and a C++ sender whose precise source has not been verified. These references do not establish the provenance or license of the complete inherited module.
- `srptools/` is the existing local copy of [srptools](https://github.com/idlesign/srptools), version 2.0.0, with its BSD-3-Clause license in `srptools/LICENSE`.
- [NumPy](https://numpy.org/) and [cryptography](https://cryptography.io/) are Python runtime dependencies installed separately.
- [FFmpeg](https://ffmpeg.org/) and [VB-Audio Virtual Cable](https://vb-audio.com/Cable/) are separately installed tools. Their binaries are outside the source package.
- The Windows output switch uses the Windows PolicyConfig COM interface, whose binary signature is documented by [AudioDeviceCmdlets](https://github.com/frgnca/AudioDeviceCmdlets/blob/master/SOURCE/IPolicyConfig.cs).

The application project license is pending the owner's choice and upstream review. This private preview is not a public open-source release.
