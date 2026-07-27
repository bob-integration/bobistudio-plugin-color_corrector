/* SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026 BOBI SAS, France
 * Auteur : Cyril Mazouer, pour le compte de BOBI SAS.
 *
 * cc_full_gamma.c — étage GAMMA du noyau « full » du correcteur, ISOLÉ dans sa propre unité de
 * compilation. Applique par ligne (buffer float in-place) : buf[j] = pow(buf[j]/255, ig) * 255.
 *
 * POURQUOI UN FICHIER SÉPARÉ : le pow scalaire (une passe par pixel, non vectorisée) est le
 * goulot d'étranglement — numpy, lui, vectorise np.power. Pour battre numpy il faut vectoriser le
 * pow via libmvec (_ZGVdN8vv_powf, 8 float/AVX2). GCC n'émet libmvec QUE si les maths vectorielles
 * « rapides » sont actives AU NIVEAU DE L'UNITÉ (-funsafe-math-optimizations -ffinite-math-only
 * -fno-math-errno) — un __attribute__((optimize(...))) local NE SUFFIT PAS (le commutateur « lib
 * math vectorielle disponible » n'est armé qu'en ligne de commande). On isole donc le gamma ici,
 * compilé en maths relâchées, tandis que TOUT le reste (conversion YUV↔RGB studio-range +
 * color-balance) reste dans cc_full.c en maths STRICTES (-ffp-contract=off) → bit-exact vs numpy.
 *
 * ACCURACY : libmvec powf ≈ 4 ULP flottant ; l'écart sur le pow est plus grand que le pow scalaire
 * double, mais après QUANTIFICATION finale il reste borné (mesuré ≤ quelques LSB par
 * tools/equiv_ccfull.py ; plafond dur 2 LSB 8 bits). Le gamma est de toute façon le SEUL point
 * non-exact du pipeline (cf. cc_full.c). Les maths relâchées ici n'affectent QUE le gamma.
 *
 * Build : cc -O3 -mavx2 -fopenmp-simd -funsafe-math-optimizations -ffinite-math-only
 *         -fno-math-errno (voir tools/build_ccfull.sh). JAMAIS -march=native.
 */
#include <math.h>
#include <stdint.h>

/* Gamma d'UNE ligne (w floats, in-place) : buf/255 → pow(·, ig) → ·255. Le #pragma omp simd +
 * les flags de maths relâchées de l'unité → vectorisation AVX2 via libmvec. */
void cc_gamma_row(float *restrict buf, int64_t w, float ig)
{
    #pragma omp simd
    for (int64_t j = 0; j < w; j++)
        buf[j] = powf(buf[j] / 255.0f, ig) * 255.0f;
}
