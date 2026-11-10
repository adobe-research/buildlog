#!/bin/sh
# A toy generator: gen SYMBOL [PLUGIN] < input > output
echo "symbol $1"
if [ -n "$2" ]; then cat "$2"; fi
cat
