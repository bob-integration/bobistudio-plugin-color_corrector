/* SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026 BOBI SAS, France
 * Auteur : Cyril Mazouer, pour le compte de BOBI SAS.
 *
 * cc_full.c — noyau C FUSIONNÉ de la branche « full » du correcteur de couleur (roundtrip
 * YUV→RGB→gamma par canal (+color balance)→RGB→YUV, threadé).
 *
 * Motivation (banc « fusion C/numba bat numpy 7-40× », cf. plugins/udc/tools/bwdif_deint.c et
 * memory processing-fusion-and-ht-isolation) : la branche `need_rgb` de _corriger_yuv (script.py)
 * est le SEUL chemin du correcteur sans raccourci LUT — un gamma non-linéaire par canal RGB ne se
 * réduit pas à une LUT indexée sur Y. En numpy pur elle refait ~6 passes mémoire plein cadre par
 * trame → 176-278 ms/trame (4-7× le budget 40 ms de 25p). Les PIXELS sont indépendants → le noyau
 * PARALLÉLISE par bandes de lignes CHROMA disjointes (pthreads, dépendance glibc — PAS de libgomp).
 *
 * ── STRUCTURE EN ÉTAGES (clé de la perf) ───────────────────────────────────────────────────────
 * Un pow() scalaire dans la boucle par pixel EMPÊCHE la vectorisation de TOUTE la boucle (le
 * pipeline reste scalaire, plus lent que numpy qui vectorise). On sépare donc, PAR LIGNE :
 *   étage 1 : YUV→RGB studio-range + clip  → 3 buffers float rbuf/gbuf/bbuf (VECTORISÉ AVX2) ;
 *   étage 2 : gamma par canal actif via cc_gamma_row (cc_full_gamma.c, libmvec powf VECTORISÉ) ;
 *   étage 3 : color-balance (si actif) + clip → rgb final ré-stocké ; puis Y = RGB→Y (VECTORISÉ) ;
 *   étage 4 : U/V par POINT-SAMPLING du RGB final aux positions [i·ch, j·cw] (r_off==0 seulement).
 * La chroma d'entrée est d'abord UPSAMPLÉE (plus-proche-voisin, PAS de moyenne) en pleine largeur
 * (ufull/vfull) pour que l'étage 1 lise en contigu (vectorisable). Buffers par-thread, réutilisés
 * ligne à ligne (~5·w floats, cache L1/L2).
 *
 * ── COEFFICIENTS (studio-range 16-235, ceux de yuv_to_rgb/rgb_to_yuv de script.py) ──────────────
 * ⚠ PAS ceux de mvk_rgba2yuv (FULL-range 0.299/0.587/0.114, inutilisable ici). La luma L du
 * color-balance, elle, EST en FULL-range (0.299/0.587/0.114) et pointwise RGB — reproduite telle
 * quelle. yuv→rgb UPSAMPLE nearest ; rgb→yuv SOUS-ÉCHANTILLONNE par point-sampling — exactement.
 *
 * ── EXACTITUDE ─────────────────────────────────────────────────────────────────────────────────
 * Étages 1/3/4 en FLOAT32, MÊME ORDRE que numpy, -ffp-contract=off (aucune fusion FMA) + SSE/AVX2
 * (pas de x87 étendu) → BIT-EXACT vs le repli numpy pour ces étapes. Le SEUL point non-exact est le
 * gamma (étage 2) : libmvec powf ≈ 4 ULP ≠ numpy float32 power. Après QUANTIFICATION entière finale
 * l'écart s'efface pour la quasi-totalité des pixels ; l'écart entier réel est MESURÉ par
 * tools/equiv_ccfull.py (plafond dur : max|Δ| ≤ 2 LSB 8 bits, proportionnel en 10/12 bits). Le
 * repli numpy (script.py, inchangé) reste bit-exact avec lui-même ; le noyau C n'est utilisé que
 * s'il se charge et confirme les formes, sinon repli silencieux.
 *
 * API (ctypes) : cc_full_u8 / cc_full_u16. Renvoie 0 = OK, <0 = erreur (→ repli numpy).
 *   y (h×w), u/v (uv_h×uv_w) : plans SOURCE contigus (déjà passés par LUT Y / sat / teinte amont).
 *   yo (h×w), uo/vo (uv_h×uv_w) : plans SORTIE contigus (buffers distincts des entrées).
 *   ch/cw : diviseurs chroma (422→1,2 ; 420→2,2 ; 444→1,1). sc=1<<(bd-8). maxf=(1<<bd)-1.
 *   ig[3] : 1/gamma par canal (R,G,B) ; gon[3] : 1 si gamma actif pour ce canal.
 *   cb_on : color balance actif ; cbs/cbm/cbh[3] : coefs shadows/mids/highs par canal (R,G,B).
 *   nthreads : ≤0 → auto = min(cœurs de l'affinité, CCFULL_MAX_THREADS) — le cpuset borne.
 * Thread-safety : aucune globale mutable ; bandes de lignes chroma disjointes → aucun verrou.
 *
 * Build : cc -O3 -fPIC -mavx2 -ffp-contract=off -fno-plt -pthread (tools/build_ccfull.sh), lié
 * avec cc_full_gamma.o. JAMAIS -march=native (le .so est distribué sur toute la flotte Xeon).
 */

#define _GNU_SOURCE
#include <stdint.h>
#include <pthread.h>
#include <sched.h>
#include <unistd.h>

#define CCFULL_ABI_VERSION 1
#define CCFULL_MAX_THREADS 12
#define CCFULL_MAX_W 8192      /* largeur max supportée par les buffers de ligne (sinon repli) */

/* Étage gamma vectorisé (libmvec), unité cc_full_gamma.c compilée en maths relâchées. */
extern void cc_gamma_row(float *restrict buf, int64_t w, float ig);

int cc_full_abi_version(void) { return CCFULL_ABI_VERSION; }

#define CCCLIPF(x, lo, hi) ((x) < (lo) ? (lo) : ((x) > (hi) ? (hi) : (x)))

/* Traite la BANDE de lignes CHROMA [cr0, cr1) (→ lignes luma [cr0·ch, cr1·ch)). Bandes disjointes
 * en écriture Y/U/V → aucun verrou. Buffers de ligne locaux, réutilisés. */
#define DEFINE_CCFULL_RANGE(NAME, PIX)                                                          \
static void NAME(const PIX *restrict y, const PIX *restrict u, const PIX *restrict v,            \
                 int64_t w, int64_t uv_w, int ch, int cw, float sc, float maxf,                 \
                 const float *ig, const int *gon, int cb_on,                                    \
                 const float *cbs, const float *cbm, const float *cbh,                           \
                 PIX *restrict yo, PIX *restrict uo, PIX *restrict vo,                           \
                 int64_t cr0, int64_t cr1)                                                       \
{                                                                                               \
    float rbuf[CCFULL_MAX_W], gbuf[CCFULL_MAX_W], bbuf[CCFULL_MAX_W];                            \
    float ufull[CCFULL_MAX_W], vfull[CCFULL_MAX_W];                                             \
    for (int64_t cr = cr0; cr < cr1; cr++) {                                                    \
        const PIX *restrict urow = u + cr * uv_w;                                               \
        const PIX *restrict vrow = v + cr * uv_w;                                               \
        for (int64_t cj = 0; cj < uv_w; cj++) {   /* upsample chroma nearest → pleine largeur */ \
            float uu = (float)urow[cj] / sc, vv = (float)vrow[cj] / sc;                          \
            for (int k = 0; k < cw; k++) { ufull[cj * cw + k] = uu; vfull[cj * cw + k] = vv; }   \
        }                                                                                       \
        for (int r_off = 0; r_off < ch; r_off++) {                                              \
            const int64_t i = cr * (int64_t)ch + r_off;                                         \
            const PIX *restrict yrow = y + i * w;                                               \
            PIX *restrict yorow = yo + i * w;                                                   \
            for (int64_t j = 0; j < w; j++) {   /* ÉTAGE 1 : YUV→RGB studio + clip (vectorisé) */ \
                float c = (float)yrow[j] / sc - 16.0f;                                          \
                float d = ufull[j] - 128.0f, e = vfull[j] - 128.0f;                              \
                float rr = 1.164f * c + 1.596f * e;                                             \
                float gg = 1.164f * c - 0.392f * d - 0.813f * e;                                \
                float bb = 1.164f * c + 2.017f * d;                                             \
                rbuf[j] = CCCLIPF(rr, 0.0f, 255.0f);                                            \
                gbuf[j] = CCCLIPF(gg, 0.0f, 255.0f);                                            \
                bbuf[j] = CCCLIPF(bb, 0.0f, 255.0f);                                            \
            }                                                                                   \
            if (gon[0]) cc_gamma_row(rbuf, w, ig[0]);   /* ÉTAGE 2 : gamma (libmvec vectorisé) */ \
            if (gon[1]) cc_gamma_row(gbuf, w, ig[1]);                                            \
            if (gon[2]) cc_gamma_row(bbuf, w, ig[2]);                                            \
            for (int64_t j = 0; j < w; j++) {   /* ÉTAGE 3 : color-balance + clip + Y (vect.) */  \
                float rr = rbuf[j], gg = gbuf[j], bb = bbuf[j];                                  \
                if (cb_on) {                                                                    \
                    float L = (0.299f * rr + 0.587f * gg + 0.114f * bb) / 255.0f;               \
                    float sw = CCCLIPF(1.0f - L * 2.0f, 0.0f, 1.0f);                            \
                    float hw = CCCLIPF(L * 2.0f - 1.0f, 0.0f, 1.0f);                            \
                    float mw = 1.0f - sw - hw;                                                  \
                    rr = rr + (sw * cbs[0] + mw * cbm[0] + hw * cbh[0]) * 128.0f;               \
                    gg = gg + (sw * cbs[1] + mw * cbm[1] + hw * cbh[1]) * 128.0f;               \
                    bb = bb + (sw * cbs[2] + mw * cbm[2] + hw * cbh[2]) * 128.0f;               \
                }                                                                               \
                rr = CCCLIPF(rr, 0.0f, 255.0f);                                                 \
                gg = CCCLIPF(gg, 0.0f, 255.0f);                                                 \
                bb = CCCLIPF(bb, 0.0f, 255.0f);                                                 \
                rbuf[j] = rr; gbuf[j] = gg; bbuf[j] = bb;   /* ré-stocke pour l'étage 4 chroma */ \
                float yv = (0.257f * rr + 0.504f * gg + 0.098f * bb + 16.0f) * sc;              \
                yv = CCCLIPF(yv, 0.0f, maxf);                                                   \
                yorow[j] = (PIX)yv;                                                             \
            }                                                                                   \
            if (r_off == 0) {   /* ÉTAGE 4 : U/V = point-sampling du RGB final [i·ch, j·cw] */    \
                for (int64_t cj = 0; cj < uv_w; cj++) {                                         \
                    float rr = rbuf[cj * cw], gg = gbuf[cj * cw], bb = bbuf[cj * cw];           \
                    float uv = (-0.148f * rr - 0.291f * gg + 0.439f * bb + 128.0f) * sc;        \
                    float vv = ( 0.439f * rr - 0.368f * gg - 0.071f * bb + 128.0f) * sc;        \
                    uv = CCCLIPF(uv, 0.0f, maxf);                                               \
                    vv = CCCLIPF(vv, 0.0f, maxf);                                               \
                    uo[cr * uv_w + cj] = (PIX)uv;                                               \
                    vo[cr * uv_w + cj] = (PIX)vv;                                               \
                }                                                                               \
            }                                                                                   \
        }                                                                                       \
    }                                                                                           \
}

DEFINE_CCFULL_RANGE(ccfull_range_u8,  uint8_t)
DEFINE_CCFULL_RANGE(ccfull_range_u16, uint16_t)

/* Nb de threads effectif : nthreads>0 imposé, sinon min(cœurs de l'affinité, MAX). */
static int cc_nthreads(int nthreads)
{
    if (nthreads > 0)
        return nthreads > CCFULL_MAX_THREADS ? CCFULL_MAX_THREADS : nthreads;
    int n = 0;
    cpu_set_t set;
    if (sched_getaffinity(0, sizeof(set), &set) == 0)
        n = CPU_COUNT(&set);
    if (n <= 0) {
        long online = sysconf(_SC_NPROCESSORS_ONLN);
        n = online > 0 ? (int)online : 1;
    }
    return n > CCFULL_MAX_THREADS ? CCFULL_MAX_THREADS : n;
}

/* Lanceur pthreads pour les deux profondeurs. Répartit [0, uv_h) en bandes de lignes CHROMA
 * contiguës DISJOINTES : threads 0..nt-2 en parallèle, dernière bande sur le thread appelant. */
#define DEFINE_CCFULL_LAUNCH(NAME, PIX, RANGE)                                                  \
typedef struct {                                                                                \
    const PIX *y, *u, *v; int64_t w, uv_w; int ch, cw; float sc, maxf;                          \
    const float *ig; const int *gon; int cb_on;                                                 \
    const float *cbs, *cbm, *cbh; PIX *yo, *uo, *vo; int64_t cr0, cr1;                           \
} NAME##_arg;                                                                                    \
static void *NAME##_thread(void *vp) {                                                           \
    NAME##_arg *a = (NAME##_arg *)vp;                                                            \
    RANGE(a->y, a->u, a->v, a->w, a->uv_w, a->ch, a->cw, a->sc, a->maxf,                         \
          a->ig, a->gon, a->cb_on, a->cbs, a->cbm, a->cbh, a->yo, a->uo, a->vo,                  \
          a->cr0, a->cr1);                                                                       \
    return (void *)0;                                                                            \
}                                                                                                \
int NAME(const PIX *y, const PIX *u, const PIX *v,                                               \
         int64_t h, int64_t w, int64_t uv_h, int64_t uv_w,                                       \
         int ch, int cw, float sc, float maxf,                                                   \
         const float *ig, const int *gon, int cb_on,                                             \
         const float *cbs, const float *cbm, const float *cbh,                                   \
         PIX *yo, PIX *uo, PIX *vo, int nthreads)                                                \
{                                                                                                \
    if (h <= 0 || w <= 0 || uv_h <= 0 || uv_w <= 0 || ch <= 0 || cw <= 0) return -1;             \
    if (uv_h * (int64_t)ch != h || uv_w * (int64_t)cw != w) return -2;   /* forme → repli */     \
    if (w > CCFULL_MAX_W) return -3;                                                             \
    int nt = cc_nthreads(nthreads);                                                              \
    if ((int64_t)nt > uv_h) nt = (int)uv_h;                                                      \
    if (nt < 1) nt = 1;                                                                          \
    int64_t chunk = (uv_h + nt - 1) / nt;                                                        \
    pthread_t th[CCFULL_MAX_THREADS];                                                            \
    NAME##_arg args[CCFULL_MAX_THREADS];                                                         \
    int started = 0;                                                                             \
    for (int t = 0; t + 1 < nt; t++) {                                                           \
        int64_t cr0 = (int64_t)t * chunk, cr1 = cr0 + chunk;                                     \
        if (cr0 >= uv_h) break;                                                                  \
        if (cr1 > uv_h) cr1 = uv_h;                                                              \
        args[started] = (NAME##_arg){y, u, v, w, uv_w, ch, cw, sc, maxf, ig, gon, cb_on,         \
                                     cbs, cbm, cbh, yo, uo, vo, cr0, cr1};                        \
        if (pthread_create(&th[started], (void *)0, NAME##_thread, &args[started]) != 0)          \
            RANGE(y, u, v, w, uv_w, ch, cw, sc, maxf, ig, gon, cb_on,                             \
                  cbs, cbm, cbh, yo, uo, vo, cr0, cr1);   /* repli inline si create échoue */     \
        else                                                                                     \
            started++;                                                                           \
    }                                                                                            \
    int64_t last0 = (int64_t)(nt - 1) * chunk;   /* dernière bande sur le thread appelant */     \
    if (last0 < uv_h)                                                                            \
        RANGE(y, u, v, w, uv_w, ch, cw, sc, maxf, ig, gon, cb_on,                                 \
              cbs, cbm, cbh, yo, uo, vo, last0, uv_h);                                            \
    for (int t = 0; t < started; t++)                                                            \
        pthread_join(th[t], (void *)0);                                                          \
    return 0;                                                                                    \
}

DEFINE_CCFULL_LAUNCH(cc_full_u8,  uint8_t,  ccfull_range_u8)
DEFINE_CCFULL_LAUNCH(cc_full_u16, uint16_t, ccfull_range_u16)
