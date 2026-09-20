#!/usr/bin/env python3
"""Fully automatic pickup order (Hangzhou / Ningbo / Shanghai).

Safari is only the logged-in session (cookies + x-aos-stk). Public
pickup-message polls stock; checkout lock/fill/place only when OPEN.

    python3 scripts/auto_order.py              # public stock poll; checkout only when OPEN
    python3 scripts/auto_order.py --no-place   # stop on review page
    python3 scripts/auto_order.py --once       # one public stock poll
    python3 scripts/auto_order.py 2            # public poll every 2s
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pickup_stock import fetch_pickup, fmt as stock_fmt

try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
STORES = json.loads((ROOT / "config" / "stores.json").read_text())
PRODUCT = json.loads((ROOT / "config" / "product.json").read_text())
BUYER = json.loads((ROOT / "config" / "buyer.json").read_text())

TARGET = [s["storeId"] for s in sorted(STORES["targetStores"], key=lambda s: s["priority"])]
TARGET_NAMES = {s["storeId"]: s["name"] for s in STORES["targetStores"]}
REJECT = set(STORES["rejectStores"])
GEO = {
    s["storeId"]: {
        "state": s["state"],
        "city": s["city"],
        "district": s["district"],
        "searchInput": s["searchInput"],
    }
    for s in STORES["targetStores"]
}

JS = r"""
(function(){
  const cmd = __CMD__;
  const storeId = __STORE__;
  const slot = __SLOT__;
  const buyer = __BUYER__;
  let stk = __STK__;

  const TARGET = __STORE_IDS__;
  const TARGET_NAMES = __STORE_NAMES__;
  const GEO = __STORE_GEO__;
  const REJECT = __STORE_REJECT__;

  function readInit(){
    const el = document.getElementById('init_data');
    if (!el) return null;
    try { return JSON.parse(el.textContent); } catch(e) { return null; }
  }
  function initStk(){
    const d = readInit();
    return (d && d.meta && d.meta.h && d.meta.h['x-aos-stk']) || '';
  }
  function modelPage(){
    const d = readInit();
    return (d && d.meta && d.meta.h && d.meta.h['x-aos-model-page']) || 'checkoutPage';
  }

  function storeGeo(selectStore){
    return GEO[selectStore] || GEO['R471'] || {
      state: '浙江', city: '杭州', district: '上城区', searchInput: '浙江 杭州 上城区'
    };
  }

  function storeBody(selectStore){
    const g = storeGeo(selectStore);
    const pcd = g.state + ' ' + g.city + ' ' + g.district;
    const p = 'checkout.fulfillment.pickupTab.pickup.storeLocator.';
    const a = p + 'address.stateCitySelectorForCheckout.';
    return [
      'checkout.fulfillment.fulfillmentOptions.selectFulfillmentLocation=RETAIL',
      p + 'showAllStores=false',
      p + 'selectStore=' + encodeURIComponent(selectStore),
      p + 'searchInput=' + encodeURIComponent(g.searchInput),
      a + 'city=' + encodeURIComponent(g.city),
      a + 'state=' + encodeURIComponent(g.state),
      a + 'provinceCityDistrict=' + encodeURIComponent(pcd),
      a + 'countryCode=CN',
      a + 'district=' + encodeURIComponent(g.district)
    ].join('&');
  }

  function withSlot(selectStore, s){
    const t = 'checkout.fulfillment.pickupTab.pickup.timeSlot.dateTimeSlots.';
    return storeBody(selectStore) + '&' + [
      t + 'startTime=' + encodeURIComponent(s.startTime || ''),
      t + 'displayEndTime=',
      t + 'timeSlotType=',
      t + 'isRecommended=false',
      t + 'endTime=' + encodeURIComponent(s.endTime || ''),
      t + 'date=' + encodeURIComponent(s.date || ''),
      t + 'timeSlotId=' + encodeURIComponent(s.timeSlotId || ''),
      t + 'signKey=' + encodeURIComponent(s.signKey || ''),
      t + 'timeZone=' + encodeURIComponent(s.timeZone || 'Asia/Shanghai'),
      t + 'timeSlotValue=' + encodeURIComponent(s.timeSlotValue || ''),
      t + 'dayRadio=' + encodeURIComponent(s.dayRadio || ''),
      t + 'isRestricted=',
      t + 'displayStartTime='
    ].join('&');
  }

  function emptyXhr(t0, err){
    return {
      status: 0,
      ms: Date.now() - t0,
      headStatus: null,
      redirect: null,
      stk: stk,
      loc: null,
      pageUrl: null,
      pageTitle: null,
      parsed: null,
      err: err || 'timeout'
    };
  }

  function packXhr(status, text, t0){
    let parsed = null;
    try { parsed = JSON.parse(text); } catch(e) {}
    const loc = parsed && parsed.body && parsed.body.meta && parsed.body.meta.l;
    const pageMeta = parsed && parsed.body && parsed.body.meta && parsed.body.meta.page;
    const newStk = parsed && parsed.body && parsed.body.meta && parsed.body.meta.h && parsed.body.meta.h['x-aos-stk'];
    if (newStk) stk = newStk;
    const redirect = parsed && parsed.head && parsed.head.data && parsed.head.data.url;
    return {
      status: status,
      ms: Date.now() - t0,
      headStatus: parsed && parsed.head && parsed.head.status,
      redirect: redirect || null,
      stk: stk,
      loc: loc || null,
      pageUrl: pageMeta && pageMeta.url,
      pageTitle: pageMeta && pageMeta.title,
      parsed: parsed,
      err: (!parsed) ? String(text || '').slice(0, 240) : null
    };
  }

  function aosHeaders(page){
    const h = {
      'Content-Type': 'application/x-www-form-urlencoded',
      'Accept': '*/*',
      'syntax': 'graviton',
      'x-aos-model-page': page || 'checkoutPage',
      'modelVersion': 'v2',
      'X-Requested-With': 'Fetch'
    };
    if (stk) h['x-aos-stk'] = stk;
    return h;
  }

  function xhr(path, body, page){
    if (!stk) stk = initStk();
    const t0 = Date.now();
    const x = new XMLHttpRequest();
    x.open('POST', path, false);
    const headers = aosHeaders(page);
    Object.keys(headers).forEach(function(k){ x.setRequestHeader(k, headers[k]); });
    try { x.send(body || ''); }
    catch (e) { return emptyXhr(t0, String(e)); }
    return packXhr(x.status, x.responseText, t0);
  }

  function xhrP(path, body, page, timeoutMs){
    if (!stk) stk = initStk();
    const t0 = Date.now();
    const wait = timeoutMs || 25000;
    const ac = new AbortController();
    const timer = setTimeout(function(){ try { ac.abort(); } catch (e) {} }, wait);
    const headers = aosHeaders(page);
    return fetch(path, {
      method: 'POST',
      headers: headers,
      body: body || '',
      credentials: 'same-origin',
      signal: ac.signal
    }).then(function(resp){
      return resp.text().then(function(text){
        clearTimeout(timer);
        return packXhr(resp.status, text, t0);
      });
    }).catch(function(e){
      clearTimeout(timer);
      const msg = (e && e.name === 'AbortError') ? 'timeout' : String(e);
      return emptyXhr(t0, msg);
    });
  }

  function findDude(o, depth){
    if (!o || depth > 8) return null;
    if (typeof o !== 'object') return null;
    if (o.dudeAttributeStore) return o.dudeAttributeStore;
    for (const k of Object.keys(o)) {
      const v = findDude(o[k], depth + 1);
      if (v) return v;
    }
    return null;
  }

  function pickup(parsed){
    return (((((parsed || {}).body || {}).checkout || {}).fulfillment || {}).pickupTab || {}).pickup || {};
  }

  function isPlain(o){
    return !!o && typeof o === 'object' && !Array.isArray(o);
  }
  function deepMerge(a, b){
    if (Array.isArray(b)) {
      if (Array.isArray(a) && a.length && !b.length) return a;
      return b;
    }
    if (!isPlain(b)) return (b === undefined) ? a : b;
    if (!isPlain(a)) return b;
    const out = {};
    Object.keys(a).forEach(function(k){ out[k] = a[k]; });
    Object.keys(b).forEach(function(k){ out[k] = deepMerge(out[k], b[k]); });
    return out;
  }
  function mergeParsed(acc, parsed){
    if (!parsed) return acc;
    if (!acc) return parsed;
    return {
      head: parsed.head || acc.head,
      body: deepMerge(acc.body || {}, parsed.body || {})
    };
  }

  function slotRoot(parsed){
    const ts = pickup(parsed).timeSlot || {};
    const nested = (ts.dateTimeSlots || {}).d || {};
    const flat = ts.d || {};
    if (nested.timeSlotWindows || nested.pickUpDates) return nested;
    if (flat.timeSlotWindows || flat.pickUpDates) return flat;
    if (nested.storeId) return nested;
    if (flat.storeId) return flat;
    return nested;
  }

  function dumpSlots(parsed){
    const dts = slotRoot(parsed);
    const windows = dts.timeSlotWindows || [];
    const days = [];
    for (let i = 0; i < windows.length; i++) {
      const day = windows[i] || {};
      const keys = Object.keys(day);
      const radio = keys.filter(function(k){ return k.indexOf('__') < 0; })[0] || null;
      const slots = radio ? (day[radio] || []) : [];
      days.push({
        radio: radio,
        n: Array.isArray(slots) ? slots.length : 0,
        countKey: keys.filter(function(k){ return k.indexOf('__') >= 0; })[0] || null
      });
    }
    const ts = pickup(parsed).timeSlot || {};
    return {
      storeId: dts.storeId || null,
      enable: !!dts.enableTimeSlotWindows,
      nWindows: windows.length,
      days: days,
      hasTimeSlot: !!pickup(parsed).timeSlot,
      tsKeys: Object.keys(ts),
      dtsKeys: Object.keys(dts)
    };
  }

  function firstSlot(parsed){
    const dts = slotRoot(parsed);
    const windows = dts.timeSlotWindows || [];
    const dates = dts.pickUpDates || [];
    for (let i = 0; i < windows.length; i++) {
      const day = windows[i] || {};
      const keys = Object.keys(day).filter(function(k){ return k.indexOf('__') < 0; });
      for (let k = 0; k < keys.length; k++) {
        const dayRadio = keys[k];
        const slots = day[dayRadio] || [];
        if (!Array.isArray(slots) || !slots.length) continue;
        for (let j = 0; j < slots.length; j++) {
          const s = slots[j];
          if (!s || !(s.SlotId || s.slotId) || !s.signKey) continue;
          if (s.isRestricted) continue;
          const dateObj = dates.find(function(d){ return String(d.dayOfMonth) === String(dayRadio); }) || {};
          return {
            date: dateObj.date || dts.date || '',
            dayRadio: String(dayRadio),
            startTime: s.checkInStart,
            endTime: s.checkInEnd,
            timeSlotId: s.SlotId || s.slotId,
            signKey: s.signKey,
            timeSlotValue: s.timeSlotValue,
            timeZone: s.timeZone || 'Asia/Shanghai',
            label: s.Label
          };
        }
      }
    }
    return null;
  }

  function slimStores(parsed){
    const stores = ((((pickup(parsed).storeLocator || {}).searchResults || {}).d) || {}).retailStores || [];
    return stores.map(function(s){
      return {
        storeId: s.storeId,
        storeName: s.storeName,
        storeDisabled: !!s.storeDisabled,
        quote: (s.availability || {}).storeAvailability || '',
        city: (s.retailAddress || {}).city || ''
      };
    });
  }

  function slimState(parsed){
    const sl = (pickup(parsed).storeLocator || {}).d || {};
    const ts = slotRoot(parsed);
    const dude = findDude(pickup(parsed), 0);
    const stores = slimStores(parsed);
    const watched = stores.filter(function(s){ return TARGET.indexOf(s.storeId) >= 0; });
    const rejected = REJECT.indexOf(sl.selectStore) >= 0;
    return {
      selectStore: sl.selectStore || null,
      searchInput: sl.searchInput || null,
      dude: dude,
      timeSlotStoreId: ts.storeId || null,
      enableTimeSlotWindows: !!ts.enableTimeSlotWindows,
      rejected: !!rejected,
      shanghai: false,
      hangzhou: watched,
      watched: watched,
      open: watched.filter(function(s){ return !s.storeDisabled; }),
      slot: firstSlot(parsed)
    };
  }

  function checkoutKeys(parsed){
    const chk = ((parsed || {}).body || {}).checkout || {};
    return Object.keys(chk);
  }

  function gravitonMsgs(parsed){
    const m = parsed && parsed.body && (parsed.body.messages || parsed.head && parsed.head.errors);
    if (!m) return null;
    try { return JSON.stringify(m).slice(0, 500); } catch (e) { return String(m).slice(0, 500); }
  }

  function blobOf(r){
    const loc = r && r.loc;
    const locS = Array.isArray(loc) ? loc.join(' ') : String(loc || '');
    return locS + ' ' + String((r && r.pageUrl) || '') + ' ' + String((r && r.pageTitle) || '');
  }

  function hasSection(parsed, r, key, token){
    if (checkoutKeys(parsed).indexOf(key) >= 0) return true;
    return !!(token && blobOf(r).indexOf(token) >= 0);
  }

  function fail(reason, extra){
    extra = extra || {};
    extra.ok = false;
    extra.reason = reason;
    extra.url = location.href;
    extra.stk = stk || initStk();
    return JSON.stringify(extra);
  }

  function stageOf(){
    const url = location.href;
    const d = readInit() || {};
    const chk = d.checkout || {};
    const pageUrl = ((d.meta || {}).page || {}).url || '';
    const blob = url + ' ' + pageUrl;
    if (blob.indexOf('interstitial') >= 0 || blob.indexOf('/checkout/status') >= 0) return 'paying';
    if (blob.indexOf('Review') >= 0 || chk.review) return 'review';
    if (blob.indexOf('Billing') >= 0 || chk.billing) return 'billing';
    if (blob.indexOf('PickupContact') >= 0 || chk.pickupContact) return 'contact';
    if (chk.fulfillment) return 'fulfillment';
    return 'checkout';
  }

  function overlayOn(){
    const header = document.getElementById('rs-sessionextender-overlay-header');
    if (header && header.offsetParent) return true;
    const box = document.querySelector('.rs-session-timeout');
    return !!(box && box.offsetParent && (box.innerText || '').trim());
  }
  function pokeActivity(){
    try {
      window.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true, view:window}));
      window.dispatchEvent(new KeyboardEvent('keydown', {bubbles:true, cancelable:true, key:'Shift'}));
      window.dispatchEvent(new Event('scroll', {bubbles:true}));
    } catch (e) {}
  }
  function clickExtend(){
    const btn = document.getElementById('checkout-session-extendSession')
      || document.querySelector('.rs-sessionextender-overlay-confirm button')
      || document.querySelector('.rs-session-timeout button')
      || document.querySelector('.ase-overlay-popup button');
    if (!btn) return false;
    try { btn.click(); return true; } catch (e) { return false; }
  }
  function sessionTtl(){
    const d = readInit() || {};
    const sess = (((d.checkout || {}).session || {}).d) || {};
    return {ttl: sess.ttl || null, canExtend: !!sess.canExtend, alertMs: sess.alertMs || null, interactionMs: sess.interactionMs || null};
  }

  if (cmd === 'where') {
    const sess = sessionTtl();
    return JSON.stringify({ok:true, url:location.href, title:document.title, stk: initStk(), model: modelPage(), stage: stageOf(), keys: Object.keys(((readInit()||{}).checkout)||{}), overlay: overlayOn(), ttl: sess.ttl});
  }

  if (cmd === 'appleKeepAlive') {
    // Best-effort Apple ID keep-warm + early expiry detector. A synchronous
    // same-origin authenticated GET: if it bounces to sign-in, the Apple ID login
    // has already expired — caught proactively here instead of at placeOrder.
    // Whether it slides the myacinfo session is unverifiable from JS (Set-Cookie
    // is invisible to page script), but authenticated activity is the usual way
    // to keep a sliding session warm. `/shop/checkout` is same-origin on secure*;
    // swap the path if a capture reveals Apple's real session heartbeat.
    const t0 = Date.now();
    const x = new XMLHttpRequest();
    let err = null;
    try {
      x.open('GET', '/shop/checkout', false);
      x.setRequestHeader('X-Requested-With', 'Fetch');
      x.send();
    } catch (e) { err = String(e); }
    const finalUrl = x.responseURL || '';
    const body = String(x.responseText || '');
    // Catch both a redirect to sign-in AND the "你的操作已超时…已为你退出了登录" idle
    // logout page (which lives on /shop/sorry and would otherwise slip past a
    // URL-only check).
    const loggedOut = /signin|appleid|idmsa|authenticate/i.test(finalUrl + ' ' + location.href)
      || /退出了登录|已为你退出|请重新登录|重新登录你的|操作已超时/.test(body);
    return JSON.stringify({
      ok: !err && !loggedOut,
      cmd: 'appleKeepAlive',
      status: err ? 0 : x.status,
      finalUrl: finalUrl,
      loggedOut: !!loggedOut,
      err: err,
      ms: Date.now() - t0
    });
  }

  if (cmd === 'goBag') {
    location.href = 'https://www.apple.com.cn/shop/bag';
    return JSON.stringify({ok:true, cmd:'goBag'});
  }

  if (location.href.indexOf('session_expired') >= 0 || location.href.indexOf('/shop/sorry') >= 0) {
    return fail('session_expired');
  }

  if (cmd === 'bagCheckout') {
    if (location.href.indexOf('/shop/bag') < 0) return fail('not on bag');
    stk = initStk();
    const r = xhr('/shop/bagx/checkout_now?_a=checkout&_m=shoppingCart.actions', '', 'cart');
    if (r.redirect) {
      location.href = r.redirect.indexOf('http') === 0 ? r.redirect : (location.origin + r.redirect);
    } else {
      location.href = 'https://secure6.www.apple.com.cn/shop/checkout';
    }
    return JSON.stringify({ok:true, cmd:'bagCheckout', status:r.status, redirect:r.redirect, stk:r.stk});
  }

  if (location.href.indexOf('/shop/checkout') < 0) {
    return fail('not on checkout');
  }

  if (!stk) stk = initStk();
  if (!stk) return fail('no x-aos-stk');

  if (cmd === 'retail') {
    const r = xhr(
      '/shop/checkoutx/fulfillment?_a=selectFulfillmentLocationAction&_m=checkout.fulfillment.fulfillmentOptions',
      'checkout.fulfillment.fulfillmentOptions.selectFulfillmentLocation=RETAIL'
    );
    if (r.status !== 200 || !r.parsed) return fail('retail failed', {status:r.status, err:r.err});
    const st = slimState(r.parsed);
    return JSON.stringify({ok:true, cmd:'retail', stk:r.stk, keys:checkoutKeys(r.parsed), state:st, title:r.pageTitle});
  }

  if (cmd === 'session') {
    const before = overlayOn();
    const clicked = clickExtend();
    pokeActivity();
    let r = xhr('/shop/checkoutx/session?_a=extendSession&_m=checkout.session', '');
    if (r.status !== 200) {
      const r2 = xhr('/shop/checkoutx/session?_a=extendSessionUrl&_m=checkout.session', '');
      if (r2.status === 200) r = r2;
    }
    pokeActivity();
    const sess = ((((r.parsed || {}).body || {}).checkout || {}).session || {}).d || {};
    return JSON.stringify({
      ok: r.status === 200,
      cmd: 'session',
      status: r.status,
      stk: r.stk,
      overlay: before,
      overlayNow: overlayOn(),
      clicked: clicked,
      ttl: sess.ttl || sessionTtl().ttl,
      url: location.href
    });
  }

  if (cmd === 'search' || cmd === 'select') {
    const use = storeId || 'R471';
    if (REJECT.indexOf(use) >= 0) return fail('refusing non-target store', {storeId:use});
    if (TARGET.indexOf(use) < 0) return fail('store not in whitelist', {storeId:use});
    const a = (cmd === 'select') ? 'select' : 'search';
    const r = xhr(
      '/shop/checkoutx/fulfillment?_a=' + a + '&_m=checkout.fulfillment.pickupTab.pickup.storeLocator',
      storeBody(use).replace('checkout.fulfillment.fulfillmentOptions.selectFulfillmentLocation=RETAIL&','')
    );
    if (r.status !== 200 || !r.parsed) return fail(a + ' failed', {status:r.status, err:r.err});
    const st = slimState(r.parsed);
    const locked = lockedFor(use, st);
    return JSON.stringify({ok:true, cmd:a, requestedStore:use, stk:r.stk, locked:!!locked, state:st, keys:checkoutKeys(r.parsed), ms:r.ms, status:r.status});
  }

  if (cmd === 'continueFulfillment') {
    const use = storeId;
    if (!use || REJECT.indexOf(use) >= 0 || TARGET.indexOf(use) < 0) return fail('bad store for continue', {storeId:use});
    if (!slot || !slot.timeSlotId || !slot.signKey) return fail('missing timeSlot', {slot:slot});
    const r = xhr(
      '/shop/checkoutx/fulfillment?_a=continueFromFulfillmentToPickupContact&_m=checkout.fulfillment',
      withSlot(use, slot)
    );
    if (r.status !== 200 || !r.parsed) return fail('continueFulfillment failed', {status:r.status, err:r.err});
    const keys = checkoutKeys(r.parsed);
    const ok = hasSection(r.parsed, r, 'pickupContact', 'PickupContact');
    return JSON.stringify({
      ok: !!ok,
      cmd: 'continueFulfillment',
      stk: r.stk,
      keys: keys,
      title: r.pageTitle,
      loc: r.loc,
      pageUrl: r.pageUrl,
      msgs: gravitonMsgs(r.parsed),
      state: slimState(r.parsed)
    });
  }

  if (cmd === 'continueContact') {
    const p = 'checkout.pickupContact.';
    const body = [
      p + 'selfPickupContact.selfContact.address.lastName=' + encodeURIComponent(buyer.lastName || ''),
      p + 'selfPickupContact.selfContact.address.firstName=' + encodeURIComponent(buyer.firstName || ''),
      p + 'selfPickupContact.selfContact.address.emailAddress=' + encodeURIComponent(buyer.emailAddress || ''),
      p + 'selfPickupContact.selfContact.address.verificationModule.verificationToken=',
      p + 'selfPickupContact.nationalIdSelf.nationalIdSelf=' + encodeURIComponent(buyer.nationalIdSelf || ''),
      p + 'eFapiaoSelector.selectFapiao=' + encodeURIComponent(buyer.selectFapiao || 'e_personal_fdf'),
      p + 'eFapiaoSelector.ePersonalFapiao.invoiceHeader=' + encodeURIComponent(buyer.invoiceHeader || '')
    ].join('&');
    const r = xhr('/shop/checkoutx?_a=continueFromPickupContactToBilling&_m=checkout.pickupContact', body);
    if (r.status !== 200 || !r.parsed) return fail('continueContact failed', {status:r.status, err:r.err});
    const keys = checkoutKeys(r.parsed);
    return JSON.stringify({ok: keys.indexOf('billing') >= 0, cmd:'continueContact', stk:r.stk, keys:keys, title:r.pageTitle});
  }

  if (cmd === 'selectAlipay') {
    const r = xhr(
      '/shop/checkoutx/billing?_a=selectBillingOptionAction&_m=checkout.billing.billingOptions',
      'checkout.billing.billingOptions.selectBillingOption=' + encodeURIComponent(buyer.billing || 'ALIPAY')
    );
    if (r.status !== 200 || !r.parsed) return fail('selectAlipay failed', {status:r.status, err:r.err});
    return JSON.stringify({ok:true, cmd:'selectAlipay', stk:r.stk, keys:checkoutKeys(r.parsed), title:r.pageTitle});
  }

  if (cmd === 'continueBilling') {
    const r = xhr(
      '/shop/checkoutx/billing?_a=continueFromBillingToReview&_m=checkout.billing',
      'checkout.billing.billingOptions.selectBillingOption=' + encodeURIComponent(buyer.billing || 'ALIPAY')
    );
    if (r.status !== 200 || !r.parsed) return fail('continueBilling failed', {status:r.status, err:r.err});
    const keys = checkoutKeys(r.parsed);
    return JSON.stringify({ok: keys.indexOf('review') >= 0, cmd:'continueBilling', stk:r.stk, keys:keys, title:r.pageTitle});
  }

  if (cmd === 'placeOrder') {
    let r = xhr('/shop/checkoutx/review?_a=continueFromReviewToProcess&_m=checkout.review.placeOrder', '');
    if (!(r.headStatus === 302 || (r.redirect && String(r.redirect).indexOf('/shop/checkout') >= 0))) {
      const r2 = xhr('/shop/checkoutx/review?_a=continueFromReviewToProcess&_m=checkout.review', '');
      if (r2.headStatus === 302 || r2.redirect) r = r2;
    }
    const url = r.redirect || r.pageUrl || null;
    if (url) {
      const abs = url.indexOf('http') === 0 ? url : (location.origin + url);
      location.href = abs;
    }
    const ok = r.headStatus === 302 || (r.redirect && String(r.redirect).indexOf('/shop/checkout') >= 0);
    return JSON.stringify({
      ok: !!ok,
      cmd: 'placeOrder',
      status: r.status,
      headStatus: r.headStatus,
      redirect: r.redirect,
      stk: r.stk,
      keys: checkoutKeys(r.parsed),
      title: r.pageTitle
    });
  }

  function slimXhr(r, name){
    const st = r.parsed ? slimState(r.parsed) : {};
    return {
      name: name,
      status: r.status,
      headStatus: r.headStatus,
      ms: r.ms,
      parsed: !!r.parsed,
      err: r.err || null,
      keys: r.parsed ? checkoutKeys(r.parsed) : [],
      selectStore: st.selectStore || null,
      dude: st.dude || null,
      searchInput: st.searchInput || null,
      timeSlotStoreId: st.timeSlotStoreId || null,
      enableTimeSlotWindows: !!st.enableTimeSlotWindows,
      slot: st.slot || null,
      slotDump: r.parsed ? dumpSlots(r.parsed) : null,
      redirect: r.redirect || null,
      title: r.pageTitle || null,
      loc: r.loc || null,
      pageUrl: r.pageUrl || null,
      msgs: r.parsed ? gravitonMsgs(r.parsed) : null
    };
  }

  function lockedFor(use, st){
    // selectStore matching the target is the authoritative "locked" signal from
    // the select response. dude/timeSlot are only positive confirmations — a
    // MISMATCHED dude is almost always stale from the previously selected store
    // (the merge keeps it when the select response omits dude), so it must NOT
    // veto a correct selectStore. A genuine wrong store is caught later by the
    // 'store bounced' check at continueFulfillment.
    return st.selectStore === use && TARGET.indexOf(st.selectStore) >= 0;
  }

  function doRetailP(){
    return xhrP(
      '/shop/checkoutx/fulfillment?_a=selectFulfillmentLocationAction&_m=checkout.fulfillment.fulfillmentOptions',
      'checkout.fulfillment.fulfillmentOptions.selectFulfillmentLocation=RETAIL'
    );
  }

  // Normal select/Selectdistrict is ~10s; anything past ~15s is a hang (Apple
  // overloaded at drop time). Cap the wait at 15s so a stuck call bails fast to
  // the district+select fallback instead of burning the full 25s.
  function doDistrictP(use){
    const m = 'checkout.fulfillment.pickupTab.pickup.storeLocator.address.stateCitySelectorForCheckout';
    return xhrP(
      '/shop/checkoutx/fulfillment?_a=Selectdistrict&_m=' + m,
      storeBody(use).replace('checkout.fulfillment.fulfillmentOptions.selectFulfillmentLocation=RETAIL&',''),
      'checkoutPage',
      15000
    );
  }

  function doSelectP(a, use){
    return xhrP(
      '/shop/checkoutx/fulfillment?_a=' + a + '&_m=checkout.fulfillment.pickupTab.pickup.storeLocator',
      storeBody(use).replace('checkout.fulfillment.fulfillmentOptions.selectFulfillmentLocation=RETAIL&',''),
      'checkoutPage',
      15000
    );
  }

  // HTTP 541/503/429 = Apple edge throttling (drop-time or a hot IP), not a hard
  // failure. Retry the store-selection steps a few times with a short backoff so
  // a transient throttle doesn't waste the whole burst. Only used on repeatable
  // reads (retail/district/select), never on the state-advancing continue steps.
  async function softRetry(fn, tries){
    tries = tries || 3;
    let r;
    for (let i = 0; i < tries; i++){
      r = await fn();
      const s = r && r.status;
      if (s !== 541 && s !== 503 && s !== 429) return r;
      if (i < tries - 1) {
        await new Promise(function(res){ setTimeout(res, 350 + i * 300); });
      }
    }
    return r;
  }

  function burstKey(){ return '__applePlaceBurst'; }

  function pushStep(job, r, name, extra){
    const s = slimXhr(r, name);
    if (extra) Object.keys(extra).forEach(function(k){ s[k] = extra[k]; });
    job.steps.push(s);
    return s;
  }

  function throwFail(reason, extra){
    extra = extra || {};
    extra.steps = extra.steps || ((window[burstKey()] || {}).steps);
    const err = new Error(reason);
    err.json = fail(reason, extra);
    throw err;
  }

  async function placeBurst(job){
    // Yield first so the `place` handler can return and this `do JavaScript`
    // evaluation completes BEFORE any fetch fires. A fetch started during the
    // AppleScript evaluation is reaped as an orphan when it returns -> the burst
    // died at step 1 with "TypeError: Load failed". Starting network calls on the
    // page's own macrotask (post-return) keeps them alive.
    await new Promise(function(resolve){ setTimeout(resolve, 0); });
    const use = storeId;
    const doPlace = __PLACE__;
    const tAll = job.t0;
    const wanted = storeGeo(use).searchInput;
    let r, st, locked;
    let acc = null;
    function take(parsed){
      const incoming = pickup(parsed);
      acc = mergeParsed(acc, parsed);
      if (incoming && Object.prototype.hasOwnProperty.call(incoming, 'timeSlot')) {
        pickup(acc).timeSlot = incoming.timeSlot;
      }
      const dts = slotRoot(acc);
      if (dts.storeId && dts.storeId !== use) delete pickup(acc).timeSlot;
      return acc;
    }

    function cityKey(s){
      return String(s || '').trim().split(/\s+/).slice(0, 2).join(' ');
    }

    // The session is almost always already on RETAIL (startup, every prior burst,
    // and recover all run retail), and `select` carries the store's own geo. So
    // select the store DIRECTLY first — skipping the redundant ~2-6s retail AND the
    // ~10s Selectdistrict. Only if that doesn't lock (rare: not on retail, or the
    // store didn't take) do the full retail → Selectdistrict → select recovery.
    r = await softRetry(function(){ return doSelectP('select', use); });
    st = slimState(take(r.parsed));
    locked = r.parsed ? lockedFor(use, st) : false;
    pushStep(job, r, 'select', {locked: !!locked});

    if (r.status !== 200 || !r.parsed || !locked) {
      const rr = await softRetry(doRetailP);
      pushStep(job, rr, 'retail');
      if (rr.status !== 200 || !rr.parsed) throwFail('retail failed', {status: rr.status, err: rr.err});
      take(rr.parsed);
      r = await softRetry(function(){ return doDistrictP(use); });
      pushStep(job, r, 'Selectdistrict');
      if (r.status !== 200 || !r.parsed) throwFail('geo failed', {status: r.status, err: r.err});
      st = slimState(take(r.parsed));
      r = await softRetry(function(){ return doSelectP('select', use); });
      st = slimState(take(r.parsed));
      locked = r.parsed ? lockedFor(use, st) : false;
      pushStep(job, r, 'select2', {locked: !!locked});
    }

    if (r.status !== 200 || !r.parsed) throwFail('select failed', {status: r.status, err: r.err, state: st});
    if (!locked) throwFail('select did not lock', {state: st, requestedStore: use, locked: false});

    let useSlot = st.slot || slot;
    if (!useSlot || !useSlot.timeSlotId || !useSlot.signKey) {
      // If the store has already gone OOS again, a second ~10s select is pure
      // waste — bail fast so we can get back to polling for the next window.
      const gone = (st.watched || []).filter(function(s){ return s.storeId === use; })[0] || {};
      const goneQuote = /目前不可取货|暂无供应|无货可取|不可取货/.test(String(gone.quote || ''));
      if (gone.storeDisabled || goneQuote) {
        throwFail('stock gone before slot', {
          state: st, requestedStore: use, locked: true,
          storeDisabled: gone.storeDisabled, quote: gone.quote || null,
          slotDump: dumpSlots(acc || r.parsed)
        });
      }
      // graviton select is often incremental and omits timeSlot; one extra select
      // usually materializes dateTimeSlots if the store still has windows.
      r = await softRetry(function(){ return doSelectP('select', use); });
      st = slimState(take(r.parsed));
      locked = r.parsed ? lockedFor(use, st) : locked;
      pushStep(job, r, 'selectSlot', {locked: !!locked});
      useSlot = st.slot || useSlot;
    }
    if (!useSlot || !useSlot.timeSlotId || !useSlot.signKey) {
      const hit = (st.watched || []).filter(function(s){ return s.storeId === use; })[0] || {};
      throwFail('missing timeSlot', {
        state: st,
        requestedStore: use,
        locked: true,
        slotDump: dumpSlots(acc || r.parsed),
        storeDisabled: hit.storeDisabled,
        quote: hit.quote || null
      });
    }

    r = await xhrP(
      '/shop/checkoutx/fulfillment?_a=continueFromFulfillmentToPickupContact&_m=checkout.fulfillment',
      withSlot(use, useSlot)
    );
    pushStep(job, r, 'continueFulfillment');
    if (r.status !== 200 || !r.parsed) throwFail('continueFulfillment failed', {status: r.status, err: r.err, msgs: gravitonMsgs(r.parsed)});
    const bounced = slimState(r.parsed).selectStore;
    if (bounced && bounced !== use) throwFail('store bounced', {bounced: bounced, requestedStore: use, msgs: gravitonMsgs(r.parsed)});
    const gotContact = hasSection(r.parsed, r, 'pickupContact', 'PickupContact');

    const cp = 'checkout.pickupContact.';
    const contactBody = [
      cp + 'selfPickupContact.selfContact.address.lastName=' + encodeURIComponent(buyer.lastName || ''),
      cp + 'selfPickupContact.selfContact.address.firstName=' + encodeURIComponent(buyer.firstName || ''),
      cp + 'selfPickupContact.selfContact.address.emailAddress=' + encodeURIComponent(buyer.emailAddress || ''),
      cp + 'selfPickupContact.selfContact.address.verificationModule.verificationToken=',
      cp + 'selfPickupContact.nationalIdSelf.nationalIdSelf=' + encodeURIComponent(buyer.nationalIdSelf || ''),
      cp + 'eFapiaoSelector.selectFapiao=' + encodeURIComponent(buyer.selectFapiao || 'e_personal_fdf'),
      cp + 'eFapiaoSelector.ePersonalFapiao.invoiceHeader=' + encodeURIComponent(buyer.invoiceHeader || '')
    ].join('&');
    r = await xhrP('/shop/checkoutx?_a=continueFromPickupContactToBilling&_m=checkout.pickupContact', contactBody);
    pushStep(job, r, 'continueContact');
    const gotBilling = r.parsed && hasSection(r.parsed, r, 'billing', 'Billing');
    if (r.status !== 200 || !r.parsed || !gotBilling) {
      throwFail(gotContact ? 'continueContact failed' : 'no pickupContact', {
        status: r.status,
        err: r.err,
        keys: r.parsed ? checkoutKeys(r.parsed) : [],
        msgs: r.parsed ? gravitonMsgs(r.parsed) : null,
        gotContact: !!gotContact
      });
    }

    r = await xhrP(
      '/shop/checkoutx/billing?_a=selectBillingOptionAction&_m=checkout.billing.billingOptions',
      'checkout.billing.billingOptions.selectBillingOption=' + encodeURIComponent(buyer.billing || 'ALIPAY')
    );
    pushStep(job, r, 'selectAlipay');
    if (r.status !== 200 || !r.parsed) throwFail('selectAlipay failed', {status: r.status, err: r.err});

    r = await xhrP(
      '/shop/checkoutx/billing?_a=continueFromBillingToReview&_m=checkout.billing',
      'checkout.billing.billingOptions.selectBillingOption=' + encodeURIComponent(buyer.billing || 'ALIPAY')
    );
    pushStep(job, r, 'continueBilling');
    if (r.status !== 200 || !r.parsed || !hasSection(r.parsed, r, 'review', 'Review')) {
      throwFail('continueBilling failed', {status: r.status, err: r.err, keys: r.parsed ? checkoutKeys(r.parsed) : [], msgs: r.parsed ? gravitonMsgs(r.parsed) : null});
    }

    if (!doPlace) {
      return JSON.stringify({
        ok: true,
        cmd: 'place',
        placed: false,
        locked: true,
        steps: job.steps,
        slot: useSlot,
        stk: stk,
        ms: Date.now() - tAll
      });
    }

    r = await xhrP('/shop/checkoutx/review?_a=continueFromReviewToProcess&_m=checkout.review.placeOrder', '');
    if (!(r.headStatus === 302 || (r.redirect && String(r.redirect).indexOf('/shop/checkout') >= 0))) {
      const r2 = await xhrP('/shop/checkoutx/review?_a=continueFromReviewToProcess&_m=checkout.review', '');
      if (r2.headStatus === 302 || r2.redirect) r = r2;
    }
    const jump = r.redirect || r.pageUrl || null;
    if (jump) {
      location.href = jump.indexOf('http') === 0 ? jump : (location.origin + jump);
    }
    const placed = r.headStatus === 302 || (r.redirect && String(r.redirect).indexOf('/shop/checkout') >= 0);
    pushStep(job, r, 'placeOrder');
    return JSON.stringify({
      ok: !!placed,
      cmd: 'place',
      placed: !!placed,
      locked: true,
      steps: job.steps,
      slot: useSlot,
      stk: stk,
      redirect: r.redirect,
      headStatus: r.headStatus,
      status: r.status,
      ms: Date.now() - tAll,
      reason: placed ? null : 'place failed'
    });
  }

  if (cmd === 'place') {
    const use = storeId;
    if (!use || REJECT.indexOf(use) >= 0 || TARGET.indexOf(use) < 0) {
      return fail('bad store for place', {storeId: use});
    }
    const job = window[burstKey()];
    if (job && job.running && job.store === use) {
      return JSON.stringify({ok:false, pending:true, cmd:'place', steps: job.steps || [], ms: Date.now() - job.t0});
    }
    if (job && job.done && !job.consumed && job.store === use) {
      job.consumed = true;
      return typeof job.result === 'string' ? job.result : JSON.stringify(job.result);
    }
    const fresh = {running:true, done:false, consumed:false, steps:[], t0: Date.now(), store: use, result:null};
    window[burstKey()] = fresh;
    placeBurst(fresh).then(function(result){
      fresh.result = result;
      fresh.running = false;
      fresh.done = true;
    }).catch(function(e){
      fresh.result = (e && e.json) ? e.json : fail('place exception', {err: String(e), steps: fresh.steps});
      fresh.running = false;
      fresh.done = true;
    });
    return JSON.stringify({ok:false, pending:true, cmd:'place', steps:[], ms:0});
  }

  return fail('unknown cmd ' + cmd);
})();
"""


def osascript_js(js: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(js)
        path = fh.name
    script = f'''tell application "Safari"
  if (count of windows) = 0 then return "NO_WINDOWS"
  set theTab to current tab of front window
  set checkoutTab to missing value
  set bagTab to missing value
  set expiredTab to missing value
  repeat with w in windows
    repeat with t in tabs of w
      set u to URL of t
      if u contains "/shop/checkout" and u does not contain "session_expired" and u does not contain "/shop/sorry" then
        set checkoutTab to t
      else if u contains "session_expired" or u contains "/shop/sorry" then
        set expiredTab to t
      else if u contains "/shop/bag" then
        set bagTab to t
      end if
    end repeat
  end repeat
  if checkoutTab is not missing value then
    set theTab to checkoutTab
  else if bagTab is not missing value then
    set theTab to bagTab
  else if expiredTab is not missing value then
    set theTab to expiredTab
  end if
  set js to (do shell script "cat " & quoted form of "{path}")
  return do JavaScript js in theTab
end tell'''
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    finally:
        Path(path).unlink(missing_ok=True)
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()
        raise RuntimeError(err or f"osascript exit {r.returncode}")
    return (r.stdout or "").strip()


def run(
    cmd: str,
    *,
    store: str | None = None,
    slot: dict | None = None,
    stk: str | None = None,
    do_place: bool = True,
) -> dict:
    js = (
        JS.replace("__CMD__", json.dumps(cmd))
        .replace("__STORE__", json.dumps(store))
        .replace("__SLOT__", json.dumps(slot))
        .replace("__BUYER__", json.dumps(BUYER, ensure_ascii=False))
        .replace("__STK__", json.dumps(stk))
        .replace("__STORE_IDS__", json.dumps(TARGET))
        .replace("__STORE_NAMES__", json.dumps(TARGET_NAMES, ensure_ascii=False))
        .replace("__STORE_GEO__", json.dumps(GEO, ensure_ascii=False))
        .replace("__STORE_REJECT__", json.dumps(list(REJECT)))
        .replace("__PLACE__", "true" if do_place else "false")
    )
    raw = osascript_js(js)
    if raw in ("", "missing value", "NO_WINDOWS"):
        time.sleep(0.4)
        raw = osascript_js(js)
    if raw in ("", "missing value", "NO_WINDOWS"):
        raise RuntimeError(f"safari empty: {raw!r}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"safari non-json: {raw[:400]!r}") from e


def beep() -> None:
    stock_alarm(seconds=8.0)


def stock_alarm(seconds: float = 8.0) -> None:
    """Loud repeating ping in the background so checkout is not blocked."""
    sounds = (
        "/System/Library/Sounds/Sosumi.aiff",
        "/System/Library/Sounds/Hero.aiff",
        "/System/Library/Sounds/Glass.aiff",
    )

    def loop() -> None:
        end = time.time() + seconds
        i = 0
        while time.time() < end:
            subprocess.run(["afplay", "-v", "1", sounds[i % len(sounds)]], check=False)
            i += 1
            if i == 1:
                subprocess.run(["say", "-v", "Ting-Ting", "有货，正在下单"], check=False)

    threading.Thread(target=loop, daemon=True).start()


def pay_alarm() -> None:
    """Loop a loud alarm until the user hits Enter (支付宝还要人点)."""
    stop = threading.Event()
    sounds = (
        "/System/Library/Sounds/Sosumi.aiff",
        "/System/Library/Sounds/Hero.aiff",
        "/System/Library/Sounds/Basso.aiff",
        "/System/Library/Sounds/Funk.aiff",
    )

    def loop() -> None:
        i = 0
        while not stop.wait(0.05):
            subprocess.run(["afplay", "-v", "1", sounds[i % len(sounds)]], check=False)
            i += 1
            if i % 4 == 0:
                subprocess.run(
                    ["say", "-v", "Ting-Ting", "订单已提交，请打开支付宝付款"],
                    check=False,
                )

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    print("=" * 56)
    print("已提交。去 Safari / 支付宝完成付款。")
    print("提示音会一直响，确认看到付款页后按回车关闭。")
    print("=" * 56)
    try:
        input()
    except EOFError:
        while not stop.is_set() and t.is_alive():
            time.sleep(1)
    stop.set()
    t.join(timeout=8)


_relogin_stop: threading.Event | None = None
_relogin_thread: threading.Thread | None = None


def relogin_alarm_start() -> None:
    """Persistent ring + voice + desktop notification until Apple ID re-login.

    Non-blocking: stock polling keeps running. Idempotent — a second call while
    already ringing is a no-op. Stop with relogin_alarm_stop() once auth is back.
    """
    global _relogin_stop, _relogin_thread
    if _relogin_thread is not None and _relogin_thread.is_alive():
        return
    _relogin_stop = threading.Event()
    stop = _relogin_stop
    sounds = (
        "/System/Library/Sounds/Sosumi.aiff",
        "/System/Library/Sounds/Funk.aiff",
        "/System/Library/Sounds/Basso.aiff",
    )
    subprocess.run(
        ["osascript", "-e",
         'display notification "Safari 里重新登录 Apple ID" with title "Apple ID 登录过期" sound name "Sosumi"'],
        check=False,
    )

    def loop() -> None:
        i = 0
        while not stop.wait(0.05):
            subprocess.run(["afplay", "-v", "1", sounds[i % len(sounds)]], check=False)
            i += 1
            if i % 4 == 1:
                subprocess.run(["say", "-v", "Ting-Ting", "Apple ID 登录过期，请重新登录"], check=False)

    _relogin_thread = threading.Thread(target=loop, daemon=True)
    _relogin_thread.start()
    print("!! Apple ID 登录过期 — Safari 里重新登录。提示音会一直响，登录好自动停。")


def relogin_alarm_stop() -> None:
    global _relogin_stop
    if _relogin_stop is not None and not _relogin_stop.is_set():
        _relogin_stop.set()
        print("Apple ID 登录已恢复，停止提醒。")


def jiggle_enabled() -> bool:
    return (os.environ.get("APPLE_JIGGLE") or "").strip().lower() in ("1", "on", "true", "yes")


_NUDGE_BIN = ROOT / "scripts" / "nudge"
_NUDGE_SRC = ROOT / "scripts" / "nudge.swift"


def _ensure_nudge() -> bool:
    """Build scripts/nudge from nudge.swift on first use. Returns True if usable."""
    if _NUDGE_BIN.exists():
        return True
    if not _NUDGE_SRC.exists():
        return False
    try:
        r = subprocess.run(
            ["swiftc", "-O", str(_NUDGE_SRC), "-o", str(_NUDGE_BIN)],
            capture_output=True, text=True, timeout=120,
        )
    except Exception as e:
        print(f"  nudge build: {e}")
        return False
    if r.returncode != 0:
        print(f"  nudge build failed: {(r.stderr or '').strip()[:160]}")
        return False
    return True


def real_nudge() -> bool:
    """Post a REAL mouse-move + key event straight to Safari (CGEventPostToPid) so
    Apple's idle-logout timer sees genuine interaction — without activating Safari
    or stealing focus. Needs Accessibility permission for the terminal. APPLE_JIGGLE=1.
    """
    if not _ensure_nudge():
        return False
    ok = True
    for mode in ("key", "move"):
        try:
            r = subprocess.run([str(_NUDGE_BIN), mode], capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                print(f"  nudge {mode}: {(r.stdout or r.stderr or '').strip()[:100]}")
                ok = False
        except Exception as e:
            print(f"  nudge {mode}: {e}")
            ok = False
    return ok


# Find a second Safari tab parked on the bag/account (NOT the checkout tab) to use
# as a session probe: reloading it in the background is real navigation that both
# keeps the Apple ID session warm and reveals — from the true server-rendered
# result — whether login has expired, without ever touching the checkout tab or
# bringing Safari to the front.
_PROBE_FIND = '''
  set probeTab to missing value
  repeat with w in windows
    repeat with t in tabs of w
      set u to URL of t
      if u contains "apple.com.cn" and u does not contain "/shop/checkout" and (u contains "/shop/bag" or u contains "/shop/account") then
        set probeTab to t
      end if
    end repeat
  end repeat
'''

_PROBE_READ_JS = r"""
(function(){
  var url = location.href;
  var body = '';
  try { body = (document.body && document.body.innerText || '').slice(0, 3000); } catch(e) {}
  var loggedOut =
    /signin|appleid|idmsa|authenticate|\/shop\/sorry/i.test(url) ||
    /操作已超时|已为你退出了登录|退出了登录的账户|请重新登录/.test(body);
  return JSON.stringify({url: url, loggedOut: !!loggedOut});
})();
"""


def probe_reload() -> str:
    """Reload the second (bag) tab in the background. Returns OK / NO_PROBE_TAB / …"""
    script = f'''tell application "Safari"
  if (count of windows) = 0 then return "NO_WINDOWS"
  {_PROBE_FIND}
  if probeTab is missing value then return "NO_PROBE_TAB"
  do JavaScript "location.reload()" in probeTab
  return "OK"
end tell'''
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        return (r.stdout or r.stderr or "").strip()
    except Exception as e:
        return str(e)


def probe_read() -> dict:
    """Read the bag tab's login state after a reload. {} if no probe tab / error."""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(_PROBE_READ_JS)
        path = fh.name
    script = f'''tell application "Safari"
  if (count of windows) = 0 then return "NO_WINDOWS"
  {_PROBE_FIND}
  if probeTab is missing value then return "NO_PROBE_TAB"
  set js to (do shell script "cat " & quoted form of "{path}")
  return do JavaScript js in probeTab
end tell'''
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    finally:
        Path(path).unlink(missing_ok=True)
    raw = (r.stdout or "").strip()
    if raw in ("", "NO_PROBE_TAB", "NO_WINDOWS", "missing value"):
        return {"probe": raw or "empty"}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"probe": "non-json", "raw": raw[:120]}


def pick_open(open_stores: list[dict]) -> dict | None:
    by_id = {s["storeId"]: s for s in open_stores}
    for sid in TARGET:
        if sid in by_id:
            return by_id[sid]
    return None


def fmt(s: dict) -> str:
    flag = "OOS" if s.get("storeDisabled") else "OPEN"
    return f"{s.get('storeId')} {flag} {s.get('storeName')} {s.get('quote')}"


def _print_step(s: dict) -> None:
    dump = s.get("slotDump") or {}
    days = dump.get("days") if isinstance(dump, dict) else None
    daybit = ""
    if days:
        daybit = " days=" + ",".join(
            f"{d.get('radio')}:{d.get('n')}" for d in days if isinstance(d, dict)
        )
    slot = s.get("slot") or {}
    slotbit = ""
    if slot.get("timeSlotId"):
        slotbit = f" slot={slot.get('date')} {slot.get('label') or slot.get('startTime')}"
    print(
        f"  {s.get('name')} status={s.get('status')} {s.get('ms')}ms "
        f"store={s.get('selectStore')} dude={s.get('dude')} "
        f"search={s.get('searchInput')} locked={s.get('locked')} "
        f"enableTS={s.get('enableTimeSlotWindows')} "
        f"keys={s.get('keys')} loc={s.get('loc')} msgs={s.get('msgs')} "
        f"err={s.get('err')}{slotbit}{daybit}"
    )


def checkout_now(place: bool, stk: str, store: dict, slot: dict) -> int:
    sid = store["storeId"]
    print(f"\n== PLACE {sid} {store.get('storeName')} protocol burst")
    shown = 0
    deadline = time.time() + 90.0
    r: dict = {}
    try:
        while time.time() < deadline:
            r = run("place", store=sid, slot=slot, stk=stk, do_place=place)
            steps = r.get("steps") or []
            while shown < len(steps):
                _print_step(steps[shown])
                shown += 1
            if not r.get("pending"):
                break
            time.sleep(0.25)
        else:
            print("  DROP: place burst still pending after 90s")
            return 2
    except Exception as e:
        print(f"  DROP: safari {e}")
        err = str(e)
        if "session_expired" in err or "safari empty" in err or "safari non-json" in err:
            return 5
        return 2
    leftover = (r.get("steps") or [])[shown:]
    for s in leftover:
        _print_step(s)
    print(
        f"  result ok={r.get('ok')} placed={r.get('placed')} locked={r.get('locked')} "
        f"reason={r.get('reason')} redirect={r.get('redirect')} {r.get('ms')}ms "
        f"slotDump={r.get('slotDump')} storeDisabled={r.get('storeDisabled')} quote={r.get('quote')}"
    )
    if r.get("placed"):
        beep()
        pay_alarm()
        return 0
    if r.get("ok") and not place:
        print("  --no-place: stopped on 查看订单. Safari is at review.")
        return 0
    reason = r.get("reason") or ""
    if reason in ("session_expired", "not on checkout", "no x-aos-stk"):
        print(f"  DROP: {reason}")
        return 5
    if reason in (
        "missing timeSlot",
        "stock gone before slot",
        "select did not lock",
        "retail failed",
        "select failed",
        "geo failed",
        "search failed",
        "store bounced",
    ):
        print(f"  DROP: {reason}")
        return 2
    print(f"  DROP: {reason or 'place failed'}")
    return 3


def main() -> int:
    once = "--once" in sys.argv
    place = "--no-place" not in sys.argv
    test = "--test" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    interval = 0.12
    if args:
        interval = max(0.08, float(args[0]))

    from pickup_stock import load_nodes, ANCHORS, _no_direct, iphone18_all_skus

    # Normal: only fire on the exact bag item (512 冰川蓝). Test: fire on ANY iPhone
    # 18 Pro / Pro Max in stock so the burst runs often — but checkout still buys
    # whatever is in the bag, so it only PLACES when the bag item is actually
    # available at that store (else it safely fails at select/timeslot).
    watch_skus = iphone18_all_skus() if test else [PRODUCT["phone"]["partNumber"]]

    print(f"SKU {PRODUCT['phone']['partNumber']} + {PRODUCT['appleCarePlus']['partNumber']} = {PRODUCT['cart']['totalDisplay']}")
    if test:
        print("=" * 56)
        print(f"*** TEST 模式：任意 18 Pro/Pro Max 有货就触发抢单（监控 {len(watch_skus)} 个 SKU）")
        print("*** 结账买的仍是购物袋里的目标；真下单会走到付款页等你支付。别拿它当日常抢购。")
        print("=" * 56)

    nodes = [n["name"] for n in load_nodes()]
    print(f"stores {TARGET}  hangzhou-first then 宁波/上海")
    print(f"anchors {ANCHORS}  (每枪合并锚点、覆盖全 11 家；早退通常 2 枪)")
    print("burst=v5 district→select (no search)  slot-retry  session=60s recover=bag")
    direct_note = "本机IP专供结账(APPLE_USE_DIRECT=1 可加入扫描)" if _no_direct() else "本机IP也在扫库存"
    print(f"nodes {nodes}  (541 cools, hard-fail×8 kicks; {direct_note})")
    from pickup_stock import tunnel_url as _tunnel_url
    if _tunnel_url():
        print("tunnel ON: 轮换隧道并行扫(每请求换IP、不踢不冷却、超时6s) — 免费代理快但541，隧道稳做兜底")
    print(f"buyer {BUYER['lastName']}{BUYER['firstName']} {BUYER['emailAddress']} pay {BUYER['billing']}")
    print(f"place_order={'YES' if place else 'NO (stop at review)'}")
    print("=" * 56)

    def wait_where(pred, timeout: float = 12.0, interval: float = 0.6) -> dict:
        end = time.time() + timeout
        last: dict = {}
        while time.time() < end:
            try:
                last = run("where")
            except Exception as e:
                last = {"err": str(e), "url": ""}
            url = last.get("url") or ""
            if pred(last, url):
                return last
            time.sleep(interval)
        return last

    def checkout_alive(where: dict) -> bool:
        url = where.get("url") or ""
        reason = where.get("reason") or ""
        if where.get("err"):
            return False
        if reason in ("session_expired", "not on checkout", "no x-aos-stk"):
            return False
        if "session_expired" in url or "/shop/sorry" in url:
            return False
        if "signin" in url.lower() or "login" in url.lower() or "appleid" in url.lower():
            return False
        return "/shop/checkout" in url

    def recover_checkout(stk: str | None) -> tuple[str | None, dict]:
        print("recover checkout session")
        try:
            where = run("where")
        except Exception as e:
            print(f"  where: {e}")
            where = {"err": str(e), "url": ""}
        url = where.get("url") or ""
        if "signin" in url.lower() or "login" in url.lower() or "appleid" in url.lower():
            print("Apple ID 登录已过期。Safari 里重新登录后再继续。")
            relogin_alarm_start()
            return stk, where
        if checkout_alive(where):
            relogin_alarm_stop()
            if where.get("overlay"):
                try:
                    ses = run("session", stk=stk)
                    print(
                        f"  overlay extend ok={ses.get('ok')} clicked={ses.get('clicked')} "
                        f"ttl={ses.get('ttl')}"
                    )
                    stk = ses.get("stk") or stk
                except Exception as e:
                    print(f"  overlay extend: {e}")
            return stk, where

        print(f"  goBag from {url or where.get('reason') or where.get('err')}")
        try:
            run("goBag")
        except Exception as e:
            print(f"  goBag: {e}")
        where = wait_where(lambda _w, u: "/shop/bag" in u, timeout=10)
        url = where.get("url") or ""
        stk = where.get("stk") or stk
        if "/shop/bag" not in url:
            print(f"  bag not loaded {url}")
            return stk, where
        try:
            bag = run("bagCheckout", stk=stk)
            print("  bagCheckout", bag)
        except Exception as e:
            print(f"  bagCheckout: {e}")
        where = wait_where(
            lambda _w, u: "/shop/checkout" in u and "session_expired" not in u,
            timeout=14,
        )
        stk = where.get("stk") or stk
        url = where.get("url") or ""
        if checkout_alive(where):
            try:
                retail = run("retail", stk=stk)
                stk = retail.get("stk") or stk
                print(
                    "  retail after recover",
                    retail.get("ok"),
                    (retail.get("state") or {}).get("searchInput"),
                )
            except Exception as e:
                print(f"  retail after recover: {e}")
        else:
            print(f"  recover failed, still {url or where.get('reason')}")
            subprocess.run(["say", "-v", "Ting-Ting", "会话掉了，正在恢复"], check=False)
        return stk, where

    where = run("where")
    print("safari", where.get("url"), where.get("title"))
    stk = where.get("stk")
    stk, where = recover_checkout(stk)
    url = where.get("url") or ""
    stk = where.get("stk") or stk
    stage = where.get("stage") or ""
    print("stage", stage, "keys", where.get("keys"), "overlay", where.get("overlay"), "ttl", where.get("ttl"))

    if not checkout_alive(where):
        if "signin" in url.lower() or "login" in url.lower() or "appleid" in url.lower():
            print("Safari 在登录页。登录 Apple ID 后再跑。")
            return 1
        print("把 Safari 停在结账页（已登录即可）。会话掉了会自己从购物袋再进。")
        return 1

    if stage == "paying":
        print("已经到付款页。去 Safari / 支付宝确认即可，不要再跑抢购。")
        pay_alarm()
        return 0

    if stage in ("contact", "billing", "review"):
        print(f"已在 {stage} 步，接着填并下单（不再轮询库存）")
        if stage == "contact":
            print(f"  fill {BUYER['lastName']}{BUYER['firstName']} {BUYER['emailAddress']}")
            c2 = run("continueContact", stk=stk)
            stk = c2.get("stk") or stk
            print(f"  contact→billing ok={c2.get('ok')} keys={c2.get('keys')} reason={c2.get('reason')}")
            if not c2.get("ok"):
                return 3
            stage = "billing"
        if stage == "billing":
            print(f"  billing {BUYER.get('billing')}")
            c3 = run("selectAlipay", stk=stk)
            stk = c3.get("stk") or stk
            print(f"  selectAlipay ok={c3.get('ok')} reason={c3.get('reason')}")
            c4 = run("continueBilling", stk=stk)
            stk = c4.get("stk") or stk
            print(f"  billing→review ok={c4.get('ok')} keys={c4.get('keys')} reason={c4.get('reason')}")
            if not c4.get("ok"):
                return 3
            stage = "review"
        if stage == "review":
            if not place:
                print("  --no-place: stopped on 查看订单")
                return 0
            print("  PLACE ORDER 立即下单")
            beep()
            c5 = run("placeOrder", stk=stk)
            print(f"  place ok={c5.get('ok')} status={c5.get('status')} head={c5.get('headStatus')} redirect={c5.get('redirect')}")
            if c5.get("ok"):
                pay_alarm()
                return 0
            print("  place failed", c5)
            return 4

    try:
        retail = run("retail", stk=stk)
        stk = retail.get("stk") or stk
        print(
            "retail",
            retail.get("ok"),
            (retail.get("state") or {}).get("searchInput"),
            retail.get("reason"),
            "status=",
            retail.get("status"),
            "err=",
            (retail.get("err") or "")[:120],
        )
    except Exception as e:
        print(f"! retail: {e}")

    def keep_apple_warm() -> None:
        """Reload the 2nd (bag) tab in the background: real navigation that keeps the
        Apple ID session warm and reveals true login state — catching an idle logout
        early instead of at placeOrder. Never touches the checkout tab, never pops
        Safari to the front. Needs a 2nd Safari tab parked on /shop/bag.
        """
        rr = probe_reload()
        if rr != "OK":
            if rr == "NO_PROBE_TAB":
                print("  probe: 没找到第二个标签页 — 在 Safari 另开一个停在 /shop/bag（会话检测/保活靠它）")
            else:
                print(f"  probe reload: {rr}")
            return
        time.sleep(2.0)  # let the bag tab finish reloading before reading state
        st = probe_read()
        if st.get("loggedOut"):
            print(f"  probe: LOGGED OUT ({st.get('url') or ''}) — Apple ID 需重新登录")
            relogin_alarm_start()
        elif st.get("probe"):
            print(f"  probe read: {st.get('probe')}")
        else:
            relogin_alarm_stop()

    ticks = 0
    last_sig = None
    last_session = time.time()
    last_keepalive = time.time()
    last_jiggle = time.time()
    sku = watch_skus
    SESSION_EVERY = 60.0
    KEEPALIVE_EVERY = 120.0
    JIGGLE_EVERY = 90.0
    jiggle_on = jiggle_enabled()
    print("keep-warm: 每 120s 后台 reload 第二个标签页(/shop/bag) 保活+检测登录（不弹 Safari）")
    if jiggle_on:
        if _ensure_nudge():
            print(f"jiggle ON: 每 {int(JIGGLE_EVERY)}s 给 Safari 投真鼠标/按键(CGEventPostToPid) 防闲置登出（不弹窗，需辅助功能权限）")
            real_nudge()
        else:
            print("jiggle ON 但 nudge 编译失败（需要 swiftc / Xcode CLT）；已跳过")
    keep_apple_warm()
    while True:
        ticks += 1
        t0 = time.time()
        if jiggle_on and t0 - last_jiggle >= JIGGLE_EVERY:
            last_jiggle = time.time()
            real_nudge()
        if t0 - last_keepalive >= KEEPALIVE_EVERY:
            last_keepalive = time.time()
            keep_apple_warm()
        if t0 - last_session >= SESSION_EVERY:
            last_session = time.time()
            dead = False
            try:
                ses = run("session", stk=stk)
                stk = ses.get("stk") or stk
                ttl = ses.get("ttl")
                print(
                    f"  extendSession ok={ses.get('ok')} overlay={ses.get('overlay')} "
                    f"clicked={ses.get('clicked')} ttl={ttl} status={ses.get('status')} "
                    f"url={(ses.get('url') or '')[-60:]}"
                )
                url = ses.get("url") or ""
                low = url.lower()
                # A failed extend CALL (status 0 timeout / 403) is NOT death — the
                # session is still alive as long as we're on the checkout page with a
                # healthy ttl; it just didn't get re-extended this cycle, so retry next
                # time. Only recover on real death signals: bounced to expired/sorry/
                # signin, or ttl genuinely about to run out.
                dead = (
                    ses.get("reason") in ("session_expired", "not on checkout", "no x-aos-stk")
                    or "session_expired" in url
                    or "/shop/sorry" in url
                    or "signin" in low
                    or "appleid" in low
                    or (ttl is not None and int(ttl) < 120_000)
                )
            except Exception as e:
                print(f"  extendSession: {e}")
                dead = True
            if dead:
                stk, where = recover_checkout(stk)
                stk = where.get("stk") or stk
                url = where.get("url") or ""
                if not checkout_alive(where):
                    print(f"  session still dead ({url or where.get('reason')}); keep polling public stock")

        result = fetch_pickup(sku, base_interval=interval)
        hz = result.get("watched") or result.get("hangzhou") or []
        opened = result.get("open") or []
        sig = tuple((s.get("storeId"), s.get("storeDisabled"), s.get("quote"), s.get("pickupDisplay")) for s in hz)
        tag = "change" if sig != last_sig or once or ticks == 1 else "poll"
        last_sig = sig
        wait = float(result.get("backoff") or 0.0)
        if result.get("reason") == "idle":
            if once:
                return 0 if opened else 1
            time.sleep(max(0.0, wait or 0.02))
            continue
        shots = result.get("shots") or []
        shot_txt = " ".join(
            f"{s.get('node')}={'OK' if s.get('ok') else (s.get('reason') or s.get('status'))}/{s.get('ms')}ms"
            for s in shots
        ) or (result.get("node") or "direct")
        hzrate = result.get("hz")
        hzbit = f" {hzrate}/s" if hzrate is not None else ""
        if not result.get("ok"):
            last = " | ".join(stock_fmt(s) for s in hz)
            extra = f"  last: {last}" if last else ""
            kick = " KICK" if result.get("kicked") else ""
            print(
                f"[{time.strftime('%H:%M:%S')}] #{ticks} FAIL{kick} "
                f"n={len(shots)} pool={result.get('pool')}{hzbit} {shot_txt}{extra}"
            )
        else:
            line = " | ".join(stock_fmt(s) for s in hz) or "(no target stores)"
            print(f"[{time.strftime('%H:%M:%S')}] #{ticks} {tag} n={len(shots)} {result.get('ms')}ms{hzbit}  {line}")
            if ticks == 1 or ticks % 10 == 0:
                print(f"         {shot_txt}")

        chosen = pick_open(opened) if result.get("ok") else None
        if chosen:
            beep()
            skubit = f" [{chosen.get('sku')}]" if test else ""
            print(f"  OPEN {chosen.get('storeId')} {chosen.get('storeName')}{skubit} — checkout lock now")
            dummy = {"timeSlotId": "", "signKey": "", "date": "", "label": ""}
            rc = checkout_now(place, stk, chosen, dummy)
            try:
                from pickup_stock import _poller as _stock_poller
                if _stock_poller is not None:
                    dumped = _stock_poller.drain()
                    if dumped:
                        print(f"  drained {dumped} in-flight stock shots after checkout")
            except Exception:
                pass
            if rc == 0:
                return 0
            print(f"  checkout_now rc={rc}, keep polling public stock")
            if rc == 5:
                stk, where = recover_checkout(stk)
                stk = where.get("stk") or stk
            if once:
                return 0

        if once:
            return 0 if opened else 1
        time.sleep(max(0.0, wait))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nstopped")
        raise SystemExit(0)
