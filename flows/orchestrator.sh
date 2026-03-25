#!/bin/bash

set -e

python ../miniflow db apply bootstrap sample
python ../miniflow integrations apply namespace.hello-world
python ../miniflow jobs apply namespace.job
python ../miniflow inspect namespace.hello-world
python ../miniflow inspect namespace.hello-shell

