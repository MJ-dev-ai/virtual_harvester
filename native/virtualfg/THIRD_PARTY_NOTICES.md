# Third-party materials

`GenTL_v1_6.h` is the unmodified GenTL 1.6 C API header.
Copyright (c) 2004–2019 GenICam GenTL Subcommittee.

Retrieved from the roboception/rc_genicam_api repository:
https://github.com/roboception/rc_genicam_api/blob/master/genicam/library/CPP/include/GenTL/GenTL_v1_6.h

The header is published under the license of the EMVA GenICam Standard Group.
The GenICam license distributed with genicam 1.5.0 is included as
`licenses/GenICam_License.pdf`. See also https://www.emva.org/standards-technology/genicam/.

VirtualFG contains an original producer implementation. It does not contain or
depend on TLSimu, libVirtualFG from the GenICam example package, or MVS binaries.
Harvester, the genicam Python bindings, NumPy, and OpenCV are installed separately
and retain their respective licenses.

`vendor/nlohmann` contains the unmodified nlohmann/json 3.11.2 headers
(https://github.com/nlohmann/json), copied from the local conda package
`nlohmann_json-3.11.2-h6a678d5_0`. Copyright 2013–2022 Niels Lohmann;
MIT license, included at `vendor/nlohmann/LICENSE.MIT`. It is header-only and
requires no Python package or runtime installation.
