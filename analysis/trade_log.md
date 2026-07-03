# Strategy chart-replay — trade-by-trade simulation

Every row is a point where the deterministic strategy FIRED on the chart (no look-ahead), followed to its stop/target. Open the symbol chart at the timestamp to verify the setup.

| date (UTC) | symbol | dir | entry | outcome | R |
|---|---|---|---|---|---|
| 2026-04-13 04:00 | HK50m | SHORT | 25571.2 | stop (loss) | -1.05 |
| 2026-04-15 16:00 | USTECm | LONG | 26012.6 | target (WIN ) | +3.95 |
| 2026-04-16 02:00 | JP225m | LONG | 59584.9 | stop (loss) | -1.05 |
| 2026-04-16 11:00 | JP225m | LONG | 59239.2 | stop (loss) | -1.05 |
| 2026-04-17 08:00 | XAUUSDm | SHORT | 4795.737 | stop (loss) | -1.05 |
| 2026-04-17 14:00 | USTECm | LONG | 26697.15 | stop (loss) | -1.05 |
| 2026-04-17 16:00 | JP225m | LONG | 59869.7 | stop (loss) | -1.05 |
| 2026-04-20 05:00 | XAUUSDm | SHORT | 4790.854 | stop (loss) | -1.05 |
| 2026-04-21 00:00 | USDCHFm | SHORT | 0.77855 | stop (loss) | -1.05 |
| 2026-04-21 17:00 | XAUUSDm | SHORT | 4710.84 | stop (loss) | -1.05 |
| 2026-04-21 23:00 | USOILm | LONG | 89.715 | timeout (WIN ) | +1.44 |
| 2026-04-22 02:00 | JP225m | LONG | 59615.5 | stop (loss) | -1.05 |
| 2026-04-22 20:00 | USTECm | LONG | 26988.25 | stop (loss) | -1.05 |
| 2026-04-23 02:00 | XAUUSDm | SHORT | 4698.721 | stop (loss) | -1.05 |
| 2026-04-23 10:00 | AUDNZDm | LONG | 1.21477 | stop (loss) | -1.05 |
| 2026-04-23 18:00 | XAUUSDm | SHORT | 4701.177 | target (WIN ) | +1.95 |
| 2026-04-24 16:00 | JP225m | LONG | 59961.0 | stop (loss) | -1.05 |
| 2026-04-24 19:00 | USTECm | LONG | 27313.78 | stop (loss) | -1.05 |
| 2026-04-27 00:00 | AUDNZDm | LONG | 1.21665 | stop (loss) | -1.05 |
| 2026-04-27 03:00 | EURUSDM | LONG | 1.17292 | stop (loss) | -1.05 |
| 2026-04-28 01:00 | AUDNZDm | LONG | 1.2163 | target (WIN ) | +3.95 |
| 2026-04-28 06:00 | USOILm | LONG | 96.324 | target (WIN ) | +1.95 |
| 2026-04-28 12:00 | XAUUSDm | SHORT | 4593.803 | stop (loss) | -1.05 |
| 2026-04-29 06:00 | AUDNZDm | LONG | 1.22101 | stop (loss) | -1.05 |
| 2026-04-29 06:00 | HK50m | LONG | 26009.4 | stop (loss) | -1.05 |
| 2026-04-29 11:00 | USOILm | LONG | 100.855 | target (WIN ) | +3.95 |
| 2026-04-29 16:00 | CADJPYm | LONG | 117.218 | stop (loss) | -1.05 |
| 2026-04-29 17:00 | USDCHFm | LONG | 0.7902 | stop (loss) | -1.05 |
| 2026-04-30 12:00 | USDCHFm | SHORT | 0.7839 | target (WIN ) | +3.21 |
| 2026-04-30 16:00 | EURUSDM | LONG | 1.17249 | stop (loss) | -1.05 |
| 2026-05-01 00:00 | USOILm | LONG | 102.584 | stop (loss) | -1.05 |
| 2026-05-01 20:00 | USTECm | LONG | 27678.42 | stop (loss) | -1.05 |
| 2026-05-04 01:00 | HK50m | LONG | 26251.8 | stop (loss) | -1.05 |
| 2026-05-04 05:00 | HK50m | LONG | 26154.8 | stop (loss) | -1.05 |
| 2026-05-04 15:00 | XAUUSDm | SHORT | 4521.43 | stop (loss) | -1.05 |
| 2026-05-05 12:00 | CADJPYm | LONG | 115.816 | stop (loss) | -1.05 |
| 2026-05-05 15:00 | JP225m | LONG | 60499.0 | target (WIN ) | +1.95 |
| 2026-05-05 16:00 | USTECm | LONG | 28027.09 | target (WIN ) | +3.95 |
| 2026-05-06 02:00 | JP225m | LONG | 61043.8 | target (WIN ) | +2.84 |
| 2026-05-06 05:00 | EURUSDM | LONG | 1.17326 | target (WIN ) | +3.10 |
| 2026-05-06 07:00 | HK50m | LONG | 26197.4 | timeout (WIN ) | +0.23 |
| 2026-05-06 12:00 | JP225m | LONG | 61819.0 | target (WIN ) | +1.95 |
| 2026-05-06 13:00 | EURUSDM | LONG | 1.17513 | stop (loss) | -1.05 |
| 2026-05-06 13:00 | USDCHFm | SHORT | 0.7798 | timeout (loss) | -0.93 |
| 2026-05-06 13:00 | USTECm | LONG | 28372.67 | target (WIN ) | +3.95 |
| 2026-05-07 04:00 | JP225m | LONG | 63114.4 | stop (loss) | -1.05 |
| 2026-05-08 09:00 | EURUSDM | LONG | 1.17685 | stop (loss) | -1.05 |
| 2026-05-08 13:00 | JP225m | LONG | 63383.4 | stop (loss) | -1.05 |
| 2026-05-08 16:00 | USTECm | LONG | 29144.99 | stop (loss) | -1.05 |
| 2026-05-11 00:00 | EURUSDM | LONG | 1.17697 | stop (loss) | -1.05 |
| 2026-05-11 04:00 | USOILm | LONG | 96.836 | stop (loss) | -1.05 |
| 2026-05-11 08:00 | XAUUSDm | SHORT | 4667.517 | stop (loss) | -1.05 |
| 2026-05-12 11:00 | USOILm | LONG | 98.11 | stop (loss) | -1.05 |
| 2026-05-12 15:00 | CADJPYm | LONG | 114.999 | stop (loss) | -1.05 |
| 2026-05-12 17:00 | XAUUSDm | SHORT | 4678.929 | stop (loss) | -1.05 |
| 2026-05-13 03:00 | USTECm | LONG | 29165.91 | stop (loss) | -1.05 |
| 2026-05-13 18:00 | AUDNZDm | LONG | 1.22325 | stop (loss) | -1.05 |
| 2026-05-13 20:00 | HK50m | LONG | 26895.8 | stop (loss) | -1.05 |
| 2026-05-14 16:00 | CADJPYm | LONG | 115.265 | timeout (WIN ) | +1.39 |
| 2026-05-14 21:00 | AUDNZDm | LONG | 1.22112 | stop (loss) | -1.05 |
| 2026-05-15 01:00 | XAUUSDm | SHORT | 4613.916 | target (WIN ) | +3.77 |
| 2026-05-18 00:00 | USOILm | LONG | 102.5 | stop (loss) | -1.05 |
| 2026-05-18 16:00 | USOILm | LONG | 102.456 | stop (loss) | -1.05 |
| 2026-05-18 18:00 | XAUUSDm | SHORT | 4535.58 | stop (loss) | -1.05 |
| 2026-05-19 16:00 | XAUUSDm | SHORT | 4507.548 | stop (loss) | -1.05 |
| 2026-05-20 00:00 | EURUSDM | SHORT | 1.16009 | stop (loss) | -1.05 |
| 2026-05-20 20:00 | JP225m | LONG | 61237.2 | target (WIN ) | +3.95 |
| 2026-05-21 02:00 | EURUSDM | SHORT | 1.16172 | stop (loss) | -1.05 |
| 2026-05-21 03:00 | USDCHFm | SHORT | 0.78719 | stop (loss) | -1.05 |
| 2026-05-21 19:00 | USDCHFm | SHORT | 0.78615 | timeout (loss) | -0.46 |
| 2026-05-22 05:00 | JP225m | LONG | 63224.5 | target (WIN ) | +1.95 |
| 2026-05-22 15:00 | HK50m | SHORT | 25415.8 | stop (loss) | -1.05 |
| 2026-05-25 00:00 | USTECm | LONG | 29867.81 | stop (loss) | -1.05 |
| 2026-05-25 01:00 | JP225m | LONG | 65387.0 | stop (loss) | -1.05 |
| 2026-05-25 10:00 | AUDNZDm | LONG | 1.22081 | target (WIN ) | +2.31 |
| 2026-05-26 09:00 | XAUUSDm | SHORT | 4522.54 | target (WIN ) | +3.13 |
| 2026-05-26 11:00 | JP225m | LONG | 65520.8 | target (WIN ) | +1.95 |
| 2026-05-26 16:00 | AUDNZDm | LONG | 1.22759 | stop (loss) | -1.05 |
| 2026-05-26 23:00 | USTECm | LONG | 30056.46 | stop (loss) | -1.05 |
| 2026-05-27 03:00 | JP225m | LONG | 65682.8 | stop (loss) | -1.05 |
| 2026-05-27 11:00 | HK50m | SHORT | 25172.9 | stop (loss) | -1.05 |
| 2026-05-27 13:00 | XAUUSDm | SHORT | 4427.167 | stop (loss) | -1.05 |
| 2026-05-28 01:00 | XAUUSDm | SHORT | 4409.782 | stop (loss) | -1.05 |
| 2026-05-28 02:00 | HK50m | SHORT | 24755.0 | stop (loss) | -1.05 |
| 2026-05-28 08:00 | HK50m | SHORT | 24920.9 | stop (loss) | -1.05 |
| 2026-05-28 17:00 | CADJPYm | LONG | 115.487 | stop (loss) | -1.05 |
| 2026-05-28 19:00 | USDCHFm | SHORT | 0.78383 | target (WIN ) | +2.43 |
| 2026-05-28 23:00 | USTECm | LONG | 30265.0 | timeout (WIN ) | +1.49 |
| 2026-05-29 04:00 | JP225m | LONG | 66339.8 | target (WIN ) | +1.95 |
| 2026-05-29 17:00 | USDCHFm | SHORT | 0.78241 | stop (loss) | -1.05 |
| 2026-06-01 14:00 | USOILm | LONG | 92.973 | stop (loss) | -1.05 |
| 2026-06-01 14:00 | XAUUSDm | SHORT | 4458.671 | stop (loss) | -1.05 |
| 2026-06-01 20:00 | AUDNZDm | LONG | 1.2067 | timeout (WIN ) | +2.60 |
| 2026-06-02 18:00 | CADJPYm | LONG | 115.605 | stop (loss) | -1.05 |
| 2026-06-03 01:00 | USDCHFm | LONG | 0.78768 | target (WIN ) | +1.95 |
| 2026-06-03 01:00 | XAUUSDm | SHORT | 4475.605 | stop (loss) | -1.05 |
| 2026-06-03 02:00 | USOILm | LONG | 92.727 | stop (loss) | -1.05 |
| 2026-06-03 08:00 | EURUSDM | SHORT | 1.16077 | stop (loss) | -1.05 |
| 2026-06-03 13:00 | CADJPYm | SHORT | 115.304 | timeout (WIN ) | +1.58 |
| 2026-06-03 13:00 | EURUSDM | SHORT | 1.16026 | stop (loss) | -1.05 |
| 2026-06-03 16:00 | HK50m | SHORT | 25388.3 | target (WIN ) | +3.24 |
| 2026-06-03 19:00 | USDCHFm | LONG | 0.79207 | stop (loss) | -1.05 |
| 2026-06-04 09:00 | JP225m | LONG | 67545.4 | stop (loss) | -1.05 |
| 2026-06-05 00:00 | USOILm | SHORT | 90.641 | stop (loss) | -1.05 |
| 2026-06-05 04:00 | HK50m | SHORT | 25001.1 | target (WIN ) | +3.03 |
| 2026-06-05 13:00 | EURUSDM | SHORT | 1.15716 | target (WIN ) | +3.95 |
| 2026-06-05 14:00 | XAUUSDm | SHORT | 4369.788 | target (WIN ) | +2.01 |
| 2026-06-05 18:00 | HK50m | SHORT | 24576.6 | stop (loss) | -1.05 |
| 2026-06-07 21:00 | USDCHFm | LONG | 0.795 | target (WIN ) | +2.89 |
| 2026-06-08 04:00 | XAUUSDm | SHORT | 4312.489 | stop (loss) | -1.05 |
| 2026-06-08 05:00 | HK50m | SHORT | 24459.8 | stop (loss) | -1.05 |
| 2026-06-08 13:00 | USOILm | SHORT | 89.531 | target (WIN ) | +1.95 |
| 2026-06-09 18:00 | USOILm | SHORT | 86.815 | stop (loss) | -1.05 |
| 2026-06-09 22:00 | XAUUSDm | SHORT | 4236.965 | target (WIN ) | +1.95 |
| 2026-06-09 23:00 | HK50m | SHORT | 24375.2 | target (WIN ) | +2.50 |
| 2026-06-10 00:00 | EURUSDM | SHORT | 1.15417 | stop (loss) | -1.05 |
| 2026-06-10 06:00 | XAUUSDm | SHORT | 4203.976 | target (WIN ) | +1.95 |
| 2026-06-10 15:00 | XAUUSDm | SHORT | 4128.65 | target (WIN ) | +1.95 |
| 2026-06-11 01:00 | XAUUSDm | SHORT | 4102.099 | stop (loss) | -1.05 |
| 2026-06-11 12:00 | CADJPYm | SHORT | 114.791 | target (WIN ) | +3.95 |
| 2026-06-11 14:00 | AUDNZDm | LONG | 1.20955 | stop (loss) | -1.05 |
| 2026-06-11 22:00 | USOILm | SHORT | 84.635 | target (WIN ) | +1.95 |
| 2026-06-12 00:00 | JP225m | LONG | 66824.4 | stop (loss) | -1.05 |
| 2026-06-12 02:00 | USTECm | LONG | 29576.32 | stop (loss) | -1.05 |
| 2026-06-12 04:00 | JP225m | LONG | 66638.3 | target (WIN ) | +2.62 |
| 2026-06-12 06:00 | XAUUSDm | SHORT | 4184.002 | stop (loss) | -1.05 |
| 2026-06-15 00:00 | CADJPYm | SHORT | 114.602 | target (WIN ) | +2.40 |
| 2026-06-15 01:00 | USOILm | SHORT | 79.692 | target (WIN ) | +1.95 |
| 2026-06-15 06:00 | JP225m | LONG | 69358.8 | target (WIN ) | +1.95 |
| 2026-06-15 07:00 | USTECm | LONG | 30276.57 | stop (loss) | -1.05 |
| 2026-06-16 06:00 | HK50m | SHORT | 24390.7 | stop (loss) | -1.05 |
| 2026-06-16 15:00 | USOILm | SHORT | 75.185 | stop (loss) | -1.05 |
| 2026-06-16 16:00 | HK50m | SHORT | 24489.3 | target (WIN ) | +3.95 |
| 2026-06-16 23:00 | USOILm | SHORT | 75.814 | stop (loss) | -1.05 |
| 2026-06-17 04:00 | USTECm | LONG | 30226.89 | stop (loss) | -1.05 |
| 2026-06-17 13:00 | AUDNZDm | LONG | 1.21498 | stop (loss) | -1.05 |
| 2026-06-17 18:00 | CADJPYm | SHORT | 114.07 | stop (loss) | -1.05 |
| 2026-06-17 18:00 | EURUSDM | SHORT | 1.15386 | target (WIN ) | +2.07 |
| 2026-06-17 18:00 | HK50m | SHORT | 24197.2 | target (WIN ) | +2.08 |
| 2026-06-17 19:00 | USDCHFm | LONG | 0.8005 | stop (loss) | -1.05 |
| 2026-06-17 20:00 | USOILm | SHORT | 74.938 | stop (loss) | -1.05 |
| 2026-06-17 22:00 | EURUSDM | SHORT | 1.15042 | stop (loss) | -1.05 |
| 2026-06-18 03:00 | USDCHFm | LONG | 0.79857 | target (WIN ) | +1.95 |
| 2026-06-18 04:00 | HK50m | SHORT | 23864.0 | target (WIN ) | +3.95 |
| 2026-06-18 05:00 | JP225m | LONG | 71237.3 | target (WIN ) | +1.95 |
| 2026-06-18 08:00 | AUDNZDm | LONG | 1.21553 | target (WIN ) | +3.95 |
| 2026-06-18 09:00 | EURUSDM | SHORT | 1.14764 | target (WIN ) | +3.95 |
| 2026-06-18 11:00 | USDCHFm | LONG | 0.80463 | stop (loss) | -1.05 |
| 2026-06-18 16:00 | USDCHFm | LONG | 0.80446 | target (WIN ) | +2.66 |
| 2026-06-18 16:00 | XAUUSDm | SHORT | 4219.239 | target (WIN ) | +3.95 |
| 2026-06-19 00:00 | CADJPYm | SHORT | 113.943 | stop (loss) | -1.05 |
| 2026-06-23 06:00 | USOILm | SHORT | 72.772 | target (WIN ) | +1.95 |
| 2026-06-23 09:00 | HK50m | SHORT | 23423.6 | stop (loss) | -1.05 |
| 2026-06-23 14:00 | EURUSDM | SHORT | 1.13817 | stop (loss) | -1.05 |
| 2026-06-24 02:00 | HK50m | SHORT | 23312.4 | stop (loss) | -1.05 |
| 2026-06-24 04:00 | XAUUSDm | SHORT | 4064.569 | stop (loss) | -1.05 |
| 2026-06-24 09:00 | XAUUSDm | SHORT | 4064.635 | target (WIN ) | +1.95 |
| 2026-06-24 12:00 | CADJPYm | SHORT | 113.586 | stop (loss) | -1.05 |
| 2026-06-24 13:00 | USDCHFm | LONG | 0.81254 | stop (loss) | -1.05 |
| 2026-06-24 16:00 | USOILm | SHORT | 70.42 | stop (loss) | -1.05 |
| 2026-06-25 04:00 | JP225m | LONG | 72127.1 | stop (loss) | -1.05 |
| 2026-06-26 02:00 | USOILm | SHORT | 70.678 | target (WIN ) | +2.31 |
| 2026-06-26 05:00 | HK50m | SHORT | 22643.4 | stop (loss) | -1.05 |
| 2026-06-30 04:00 | XAUUSDm | SHORT | 3985.609 | stop (loss) | -1.05 |
| 2026-07-01 00:00 | USTECm | LONG | 30305.19 | stop (loss) | -1.05 |
| 2026-07-01 07:00 | XAUUSDm | SHORT | 3976.152 | stop (loss) | -1.05 |
| 2026-07-01 08:00 | USOILm | SHORT | 68.289 | stop (loss) | -1.05 |
| 2026-07-01 13:00 | USOILm | SHORT | 69.016 | timeout (WIN ) | +0.63 |
| 2026-07-02 02:00 | USTECm | LONG | 29990.61 | stop (loss) | -1.05 |
| 2026-07-02 11:00 | CADJPYm | SHORT | 113.611 | timeout (WIN ) | +0.66 |