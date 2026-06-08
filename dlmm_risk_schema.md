# Skema Data — DLMM Token Risk Tracker (Solana)

Tujuan: menilai risiko sebuah token **sebelum** memasang likuiditas di pool DLMM,
dengan mendeteksi konsentrasi kepemilikan, aktivitas sniper/insider, pola distribusi,
dan seberapa sering harga "memantul" (mean reversion).

Arsitektur berlapis (medallion):
- **bronze** — data mentah hasil tarik dari API, apa adanya.
- **silver** — data ter-decode & bersih, satu baris per entitas/kejadian.
- **gold** — agregasi analitik & skor risiko (output yang dipakai untuk keputusan).

Tipe data ditulis Postgres/DuckDB-friendly. Sesuaikan jika pakai ClickHouse.

---

## Sumber data (acuan untuk agent)

| Kebutuhan | Sumber | Catatan |
|---|---|---|
| DEX trades (buy/sell, wallet, USD, timestamp) | Bitquery GraphQL `DEXTrades` / `DEXTradeByTokens` | filter per token mint atau per program DLMM |
| Pool DLMM, posisi LP, deposit/withdraw | Meteora API `dlmm-api.meteora.ag` + Shyft (`meteora_dlmm_LbPair/Position/PositionV2`) | rate limit Meteora 30 RPS |
| Distribusi holder & metadata token | Birdeye / Helius | snapshot berkala |
| Program ID DLMM Meteora | `LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo` | untuk filter trade DLMM |
| Mint SOL (base) | `So11111111111111111111111111111111111111112` | denominator harga |

---

## BRONZE — data mentah

```sql
-- Setiap swap mentah dari Bitquery, disimpan apa adanya untuk reproducibility.
CREATE TABLE bronze_trades (
    ingested_at     TIMESTAMP    DEFAULT now(),
    source          TEXT,                 -- 'bitquery'
    raw             JSONB,                -- payload mentah per trade
    tx_signature    TEXT,                 -- untuk dedup
    block_time      TIMESTAMP
);

-- Event pool DLMM mentah (create, add/remove liquidity).
CREATE TABLE bronze_pool_events (
    ingested_at     TIMESTAMP    DEFAULT now(),
    source          TEXT,                 -- 'meteora' | 'shyft'
    raw             JSONB,
    pool_address    TEXT,
    event_time      TIMESTAMP
);

-- Snapshot holder mentah per token per waktu.
CREATE TABLE bronze_holder_snapshots (
    ingested_at     TIMESTAMP    DEFAULT now(),
    source          TEXT,                 -- 'birdeye' | 'helius'
    raw             JSONB,
    token_mint      TEXT,
    snapshot_time   TIMESTAMP
);
```

---

## SILVER — data bersih & ter-decode

```sql
-- Satu baris per token.
CREATE TABLE dim_token (
    token_mint      TEXT PRIMARY KEY,
    symbol          TEXT,
    name            TEXT,
    decimals        INT,
    created_at      TIMESTAMP,            -- waktu mint/launch token
    first_pool_at   TIMESTAMP,            -- pool DLMM pertama dibuat
    total_supply    NUMERIC
);

-- Satu baris per wallet (identitas + label hasil profiling, diisi di gold).
CREATE TABLE dim_wallet (
    wallet_address  TEXT PRIMARY KEY,
    first_seen      TIMESTAMP,
    last_seen       TIMESTAMP
);

-- Trade yang sudah dibersihkan & dinormalisasi. Inti dari semua analisis.
CREATE TABLE fact_trade (
    trade_id        BIGINT,
    tx_signature    TEXT,
    block_time      TIMESTAMP,
    token_mint      TEXT REFERENCES dim_token(token_mint),
    wallet_address  TEXT REFERENCES dim_wallet(wallet_address),
    side            TEXT,                 -- 'buy' | 'sell'
    base_mint       TEXT,                 -- biasanya SOL
    token_amount    NUMERIC,              -- jumlah token (sudah disesuaikan decimals)
    base_amount     NUMERIC,              -- jumlah SOL
    amount_usd      NUMERIC,
    price_usd       NUMERIC,              -- harga per token saat trade
    dex             TEXT,                 -- 'meteora_dlmm' | 'raydium' | ...
    pool_address    TEXT,
    block_slot      BIGINT,               -- penting untuk deteksi sniper se-slot
    PRIMARY KEY (tx_signature, token_mint, wallet_address, side)
);

-- Info pool DLMM.
CREATE TABLE fact_pool (
    pool_address    TEXT PRIMARY KEY,
    token_x_mint    TEXT,
    token_y_mint    TEXT,
    bin_step        INT,
    created_at      TIMESTAMP,
    creator         TEXT
);

-- Snapshot distribusi holder.
CREATE TABLE fact_holder_snapshot (
    token_mint      TEXT,
    snapshot_time   TIMESTAMP,
    wallet_address  TEXT,
    balance         NUMERIC,
    pct_supply      NUMERIC,              -- balance / total_supply
    holder_rank     INT,                  -- 1 = holder terbesar
    PRIMARY KEY (token_mint, snapshot_time, wallet_address)
);
```

---

## GOLD — agregasi & skor

```sql
-- PnL & timing per wallet PER token. Basis untuk melabeli wallet insider.
CREATE TABLE wallet_token_pnl (
    wallet_address    TEXT,
    token_mint        TEXT,
    first_buy_at      TIMESTAMP,
    last_sell_at      TIMESTAMP,
    entry_lag_sec     BIGINT,             -- first_buy_at - token.created_at (makin kecil = makin awal/sniper)
    hold_seconds      BIGINT,             -- last_sell_at - first_buy_at
    total_bought_usd  NUMERIC,
    total_sold_usd    NUMERIC,
    realized_pnl_usd  NUMERIC,            -- total_sold - total_bought (porsi yang sudah keluar)
    roi               NUMERIC,            -- realized_pnl / total_bought
    PRIMARY KEY (wallet_address, token_mint)
);

-- Profil agregat per wallet across SEMUA token. Sumber label.
CREATE TABLE wallet_profile (
    wallet_address       TEXT PRIMARY KEY,
    tokens_traded        INT,
    win_rate             NUMERIC,         -- % token dengan roi > 0
    median_hold_seconds  BIGINT,
    median_entry_lag_sec BIGINT,
    total_realized_pnl   NUMERIC,
    dump_and_run_score   NUMERIC,         -- 0..1, lihat definisi sinyal di bawah
    label                TEXT             -- 'insider' | 'sniper' | 'normal' | 'lp'
);

-- Sinyal risiko per token. Satu baris per token (di-refresh berkala).
CREATE TABLE token_signals (
    token_mint              TEXT PRIMARY KEY,
    computed_at             TIMESTAMP,
    top10_holder_pct        NUMERIC,      -- konsentrasi
    sniper_wallet_count     INT,          -- pembeli dalam N slot pertama
    sniper_buy_pct          NUMERIC,      -- % supply diborong sniper di awal
    insider_wallet_count    INT,          -- jumlah wallet berlabel insider/sniper di pembeli awal
    insider_holding_pct     NUMERIC,      -- % supply dipegang wallet insider
    net_flow_trend          NUMERIC,      -- slope (buy_usd - sell_usd) over time; negatif = distribusi
    bounce_rate             NUMERIC,      -- lihat definisi sinyal
    max_drawdown_pct        NUMERIC,
    unique_buyers_24h       INT,
    liquidity_locked        BOOLEAN
);

-- Skor akhir + verdict.
CREATE TABLE token_risk_score (
    token_mint    TEXT PRIMARY KEY,
    computed_at   TIMESTAMP,
    risk_score    NUMERIC,                -- 0..100, makin tinggi makin berbahaya untuk LP
    verdict       TEXT,                   -- 'avoid' | 'caution' | 'ok'
    breakdown     JSONB                   -- kontribusi tiap sinyal, untuk transparansi
);
```

---

## Definisi sinyal (logika untuk agent)

**entry_lag_sec** = `first_buy_at − dim_token.created_at`. Makin kecil = makin
awal masuk. Wallet yang konsisten punya lag sangat kecil di banyak token = kandidat sniper.

**sniper_wallet_count / sniper_buy_pct** — hitung wallet yang membeli dalam N slot
pertama sejak pool dibuat (mis. N = 1–3 slot, pakai `block_slot`). Banyak wallet
membeli di slot yang sama persis = pembelian terkoordinasi (bundle).

**dump_and_run_score** (per wallet, 0..1) — kombinasi: `win_rate` tinggi
+ `median_hold_seconds` sangat pendek + `median_entry_lag_sec` sangat kecil
+ `total_realized_pnl` positif besar. Normalisasi tiap komponen ke 0..1 lalu rata-rata
berbobot. Wallet skor tinggi = "masuk awal, jual cepat dengan profit" berulang kali.

**label wallet** — turunkan dari profil: `insider` jika dump_and_run_score tinggi;
`sniper` jika entry_lag konsisten di slot awal; `lp` jika lebih banyak event
add/remove liquidity daripada swap; sisanya `normal`.

**insider_wallet_count / insider_holding_pct** (per token) — berapa banyak pembeli
awal token ini yang berlabel `insider`/`sniper`, dan berapa % supply mereka pegang.
**Ini sinyal paling penting untukmu**: token yang pembeli awalnya didominasi wallet
dump-and-run = persis yang bikin posisi DLMM-mu tidak memantul.

**net_flow_trend** — regresikan `(buy_usd − sell_usd)` kumulatif terhadap waktu;
slope negatif berkelanjutan = fase distribusi (insider sedang keluar).

**bounce_rate** (metrik "memantul"-mu) — dari series harga: hitung tiap drawdown
≥ X% (mis. 30%), lalu cek apakah harga pulih ≥ Y% dari titik bawah dalam window Z
(mis. balik 50% dalam 1 jam). `bounce_rate = jumlah pemulihan / jumlah drawdown`.
Rendah = harga jatuh dan tidak balik = buruk untuk LP.

**risk_score** — gabungan tertimbang dari token_signals (arah: top10_holder_pct↑,
sniper_buy_pct↑, insider_holding_pct↑, net_flow_trend↓, bounce_rate↓, liquidity_locked=false
→ semuanya menaikkan risiko). Simpan kontribusi tiap komponen di `breakdown` agar
keputusan bisa ditelusuri.

---

## Catatan implementasi

- **Dedup** di silver pakai `tx_signature`. Bitquery archive bisa kirim ulang.
- **Decimals**: selalu sesuaikan `token_amount` dengan `dim_token.decimals` sebelum agregasi.
- **realized_pnl** hanya menghitung porsi yang sudah dijual; sisa holding bukan PnL terealisasi.
  Jika ingin unrealized, valuasi sisa balance dengan harga terkini.
- **Validasi awal**: jalankan pada 20–30 token yang sudah pernah kamu LP (untung & rugi),
  cek apakah `risk_score` memisahkan yang rugi dari yang untung sebelum dipakai live.
- Skema ini risk-reduction, bukan jaminan: wallet insider berganti alamat, jadi label
  `wallet_profile` perlu di-refresh berkala.
