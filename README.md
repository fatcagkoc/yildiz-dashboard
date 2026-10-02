# Yıldız Holding Hisse Paneli

Yıldız Holding'in Borsa İstanbul'daki 7 şirketini (ULKER, BESLR, SOKM, BIZIM, GOZDE, PENTA, MAKTK) sektör endeksleri ve BIST 100 ile karşılaştıran canlı panel.

- **Güncelleme:** GitHub Actions, borsa açıkken hafta içi saatte bir (`.github/workflows/update.yml`) `fetch_data.py` dosyasını çalıştırır, `data.json` dosyasını yeniler ve sayfayı GitHub Pages'te yayınlar.
- **Kaynaklar (ücretsiz, anahtarsız):** TradingView (fiyat/endeks geçmişi, piyasa değeri, F/K, PD/DD), Yahoo Finance (USD/TRY).
- **Elle güncelleme:** Actions sekmesi → "Verileri guncelle ve yayinla" → Run workflow.
- Bir kaynak hata verirse önceki veriler korunur; sayfa boş kalmaz.

Veriler ~15 dk gecikmelidir. Bilgi amaçlıdır, yatırım tavsiyesi değildir.
