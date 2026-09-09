"""
Live ingest: the phone's TCP stream -> the reconstruction pipeline.

The one structural rule this package exists to enforce: **the 3D model is
built from packets as they arrive, never from a file.** `source.py` hands
decoded packets to any number of subscribers; `recorder.py` is one such
subscriber that writes a replayable fixture to disk, and deleting it would
change nothing about the model. The live consumer never opens a frame file.

Layout:

    protocol.py   wire format <-> typed packets (no sockets, no state)
    clock.py      the three clock domains -> one session-relative timeline
    geodesy.py    lat/lon/alt -> local ENU metres, exact inverse of
                  georeference.enu_to_latlon
    source.py     socket server + replay driver + the packet tee
    recorder.py   fixture writer (test harness only, not a data path)
"""
