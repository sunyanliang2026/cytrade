# QMT L2 Subscription Diagnostic Probe

QMT_L2_SUBSCRIPTION_PROBE.py is a read-only native QMT model. It subscribes to one l2order stream and does not query an account, submit orders, or cancel orders. Run only one probe at a time for the same stock. QMT_L2_FOLLOW_PROBE.py remains the separate live-order test.

## Run Windows

The probe subscribes immediately when started. Start it shortly before the session open you want to observe:

- Afternoon: start before 13:00. It reports an opening observation check at 13:03.
- Next trading day morning: start before 09:30. It reports an opening observation check at 09:33.

The 13:03/09:33 check is informational and does not unsubscribe. Three consecutive callbacks whose event age exceeds 2 seconds, with event timestamps at or after the selected session open, schedule an unsubscribe control callback 50 ms later. This remains active after the opening check, so later backlog can still trigger the comparison. The probe records callbacks during and after unsubscribe, resubscribes after 300 ms, and observes the new subscription for 60 seconds before a final unsubscribe and five-second callback window. If no backlog is detected, it keeps monitoring until manually stopped.

Each callback closes over the subscription phase that registered it. A callback retained from the first subscription remains classified as an old-stream callback even after the second subscription starts. This separates late delivery after unsubscribe from callbacks owned by the new subscription.

The installed `_PyContextInfo.py` wrapper exposes `subscribe_quote(stock_code, period, dividend_type, result_type, callback)` and forwards to the native context with `stock_code, period, dividend_type, callback`. That Python layer has no `count` or `start_time` option. This does not establish whether a lower QMT layer supports a live-only start mode; the internal client log comparison below is needed for that.

The default target is 603278.SH. Change TARGET_CODE in the probe before running if a different active stock is needed. Use one target only to limit quote load.

## Log

The probe writes userdata/log/qmt_l2_subscription_probe_YYYYMMDD_603278SH.log.

- SUBSCRIBE_RETURN records the request start, return time, call duration, and subscription ID.
- STALE_CALLBACK and BACKLOG_RESET_BEGIN show the event-driven reset trigger and control timer delay. The reset requires three consecutive callbacks over the 2-second age threshold.
- CALLBACK_SAMPLE records up to 20 callbacks per phase with event time, local receive time, event age, request-to-callback time, and whether the event predates that subscription request by more than 500 ms.
- CALLBACK_WINDOW reports callback rate, stale count over 2 seconds, event-time ordering, interarrival gaps, Python handler time, and scheduled-timer lag in five-second windows.
- UNSUBSCRIBE_RETURN and UNSUBSCRIBE_WINDOW_END report callbacks during the unsubscribe call and during the 300 ms before resubscription.
- late_old_stream_callbacks counts first-subscription callbacks delivered after resubscription began. Phase 2 samples and summaries belong to the second subscription only.
- before_boundary means the event timestamp was at least 500 ms earlier than the Python subscription request. It proves the callback carried an older event timestamp; by itself it cannot distinguish a QMT replay/cache from a packet already queued inside the client. Compare the timestamp and symbol with QMT's internal subscribe coverage logs below.
- handler_* measures Python callback work, while timer_lag_ms measures how late QMT ran the scheduled logger. delay_ms is local receive time minus event time and depends on the Windows clock being synchronized.
- OPENING_OBSERVATION_CHECK means the first three minutes ended without a reset trigger at that point; monitoring continues, and a later sustained stale stream can still trigger the comparison.

To compare QMT's internal request, match the stock and times in this log against XtClient_YYYYMMDD.log entries for subscribeQuoter: L2 1802 ORDER and orderCallBack: L2 1802 ORDER COVER_ALL. Those internal records show QMT's request and coverage ranges but may not map one-to-one to the Python subscription ID.

The probe's callback path only parses the timestamp and updates bounded in-memory counters. Samples and summaries are written by scheduled callbacks, not on every market event.
