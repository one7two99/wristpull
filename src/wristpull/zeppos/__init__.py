"""Talk to a Zepp OS band (Amazfit Helio Strap) over Bluetooth Low Energy, without Gadgetbridge.

The protocol is the one Gadgetbridge implements for Zepp OS devices, verified against the strap on
2026-11-07 (``docs/strap-ble-concept.md``): a chunked, AES-encrypted channel on the ``FEE0`` GATT
service, authentication with the band's 16-byte key after an ECDH exchange on NIST B-163, and a
"fetch everything since" command per data type whose answers are the same byte layouts Gadgetbridge
stores in its ``HUAMI_*`` tables. Nothing here does I/O except :mod:`.transport` (bleak) and
:mod:`.store` (SQL); :mod:`.band` drives a :class:`~wristpull.zeppos.band.Transport` and tests
inject a fake band.

Source name of the data pulled this way: ``zepp_ble``.
"""

SOURCE = "zepp_ble"
