#!/bin/sh
# Build du noyau « full » du correcteur → tools/cc_full.so (x86_64, glibc de la machine de build).
# À builder sur un hôte Debian trixie (= glibc 2.41 de l'image bobi-compute) ou plus ancien.
# -mavx2 : safe sur toute la flotte (Xeon Scalable Gen10/6240R) ; PAS de -march=native
# (le .so est distribué, il doit charger sur tous les nœuds — cf. gardes AVX2 runtime dans script.py).
#
# DEUX unités :
#   cc_full.c        — conversion YUV↔RGB studio-range + color-balance, maths STRICTES
#                      (-ffp-contract=off) → BIT-EXACT vs le repli numpy.
#   cc_full_gamma.c  — étage gamma SEUL, maths RELÂCHÉES (-funsafe-math-optimizations
#                      -ffinite-math-only -fno-math-errno) → GCC vectorise powf via libmvec
#                      (_ZGVdN8vv_powf). Isolé pour ne PAS relâcher les maths des autres étages.
set -e
cd "$(dirname "$0")"
cc -O3 -fPIC -mavx2 -ffp-contract=off -fno-plt -Wall -Wextra -c cc_full.c -o cc_full.o
cc -O3 -fPIC -mavx2 -fopenmp-simd -funsafe-math-optimizations -ffinite-math-only -fno-math-errno \
   -Wall -Wextra -c cc_full_gamma.c -o cc_full_gamma.o
cc -shared -pthread -o cc_full.so cc_full.o cc_full_gamma.o -lm
strip cc_full.so 2>/dev/null || true
# Contrôle : la vectorisation libmvec doit être présente (sinon perf ratée silencieusement).
if command -v objdump >/dev/null 2>&1; then
    if objdump -T cc_full.so 2>/dev/null | grep -q "_ZGV.*powf"; then
        echo "OK: libmvec powf vectorisé présent"
    else
        echo "ATTENTION: pas de symbole libmvec _ZGV*powf — gamma NON vectorisé (perf dégradée)"
    fi
fi
rm -f cc_full.o cc_full_gamma.o
ls -l cc_full.so
