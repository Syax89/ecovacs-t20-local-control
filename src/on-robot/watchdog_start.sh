#!/bin/sh
# Avvia il watchdog del mini-broker (istanza singola gestita dallo script stesso).
cd /data/mdsbroker && exec python2 /data/mdsbroker/mdsbroker_watchdog.py --daemon
