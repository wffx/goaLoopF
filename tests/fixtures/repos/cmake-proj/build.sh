#!/bin/sh
set -eu

output_dir=$PWD/cmake-build/bin
mkdir -p "$output_dir"
clang \
  -fsanitize=fuzzer,undefined \
  -fprofile-instr-generate \
  -fcoverage-mapping \
  -Iinclude \
  src/harness.cpp \
  src/target.c \
  -o "$output_dir/fuzz_harness.out"
printf '[100%%] Built target fuzz_harness\n'
printf 'GOALOOP_FUZZER=%s\n' "$output_dir/fuzz_harness.out"
