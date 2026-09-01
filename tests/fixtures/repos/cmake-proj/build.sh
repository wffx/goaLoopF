#!/bin/sh
set -eu

output_dir=${GOALOOP_RUN_DIR:?}/build-output
mkdir -p "$output_dir"
clang \
  -fsanitize=fuzzer,undefined \
  -fprofile-instr-generate \
  -fcoverage-mapping \
  -Iinclude \
  src/harness.cpp \
  src/target.c \
  -o "$output_dir/cmake_fuzzer"
printf 'GOALOOP_FUZZER=%s\n' "$output_dir/cmake_fuzzer"
