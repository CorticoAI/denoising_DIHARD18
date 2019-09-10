#!/bin/bash

for entry in "$1"/*
do
    ./run_denoising.sh "$entry"
done
