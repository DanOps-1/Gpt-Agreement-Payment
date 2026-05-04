// Cloudflare Email Worker — receives mail via Email Routing catch-all,
// extracts a 6-digit OTP, stores it in KV keyed by recipient address.
//
// Bindings (set by setup_cf_email_worker.py):
//   OTP_KV       — KV namespace for {recipient → {otp, ts, from, subject}}
//   FALLBACK_TO  — (optional) plain_text. If set, forward raw email to this
//                  address as well (useful during migration off IMAP/QQ).
//
// Pipeline reads KV via CF API (CTF-reg/cf_kv_otp_provider.py).

export default {
  async email(message, env, ctx) {
    const to = (message.to || '').toLowerCase();
    const from = message.from || '';

    // Read the raw RFC822 message into a string
    let raw = '';
    try {
      const reader = message.raw.getReader();
      const decoder = new TextDecoder('utf-8', { fatal: false });
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        raw += decoder.decode(value, { stream: true });
      }
      raw += decoder.decode();
    } catch (e) {
      console.error('raw read failed:', e && e.message);
    }

    // Pull the Subject header out for fast-path matching (most OpenAI OTP
    // mails put the code right in the subject)
    const subjMatch = raw.match(/^Subject:\s*(.+?)(?:\r?\n[^\s])/ms);
    const subject = subjMatch ? subjMatch[1].trim().slice(0, 200) : '';

    // Recipient + sender address digits (zone names often contain 6 digits, fallback regex
    // false positive e.g. random@123456.example.com zone → "123456")
    const addrDigits = ((to + ' ' + from).match(/\d/g) || []).join('');
    const isFromAddr = (s) => addrDigits.length >= 6 && addrDigits.includes(s);

    // OpenAI email HTML has many brand color hex like #353740 / #10A37F, fallback
    // \b\d{6}\b grabs full-digit hex as OTP.
    // Use negative lookbehind to exclude preceding #, explicitly exclude common CSS hex contexts.
    const isHexColor = (haystack, idx) => {
      if (idx > 0 && haystack[idx - 1] === '#') return true;
      // "color:353740" / "background-color: #353740" / "bgcolor=\"353740\""
      const before = haystack.slice(Math.max(0, idx - 30), idx);
      return /(?:color|background|bgcolor|fill|stroke)\s*[:=]\s*["']?#?\s*$/i.test(before);
    };

    // OTP extraction — semantic context first to avoid grabbing tracking ids,
    // and skip any candidate that's a substring of the address digits.
    let otp = null;
    const candidates = [
      // "code is 123456", "verification code: 123456", etc.
      /(?:code(?:\s*is)?|verification|one[-\s]*time|verify|verification code)[^\d]{0,40}(\d{6})\b/gi,
      // ChatGPT subject template: "Your ChatGPT code is 123456"
      /chatgpt[^\d]{0,40}(\d{6})/gi,
      /openai[^\d]{0,40}(\d{6})/gi,
    ];
    const haystack = subject + '\n' + raw;
    for (const re of candidates) {
      let m;
      while ((m = re.exec(haystack)) !== null) {
        if (!isFromAddr(m[1]) && !isHexColor(haystack, m.index + m[0].lastIndexOf(m[1]))) {
          otp = m[1]; break;
        }
      }
      if (otp) break;
    }
    if (!otp) {
      // Body-only fallback: skip header section (after first blank line) so
      // To:/From:/Delivered-To: digits don't participate in fallback matching
      const bodyStart = raw.search(/\r?\n\r?\n/);
      const body = bodyStart >= 0 ? raw.slice(bodyStart) : raw;
      const re = /\b(\d{6})\b/g;
      let m;
      while ((m = re.exec(body)) !== null) {
        const cand = m[1];
        if (isFromAddr(cand)) continue;
        if (isHexColor(body, m.index)) continue;
        otp = cand; break;
      }
    }

    if (otp && to) {
      const payload = JSON.stringify({
        otp,
        ts: Date.now(),
        from,
        subject,
      });
      try {
        await env.OTP_KV.put(to, payload, { expirationTtl: 600 });
        console.log(`stored OTP for ${to.slice(0, 40)} (subject="${subject.slice(0, 60)}")`);
      } catch (e) {
        console.error('KV put failed:', e && e.message);
      }
    } else {
      console.log(`no OTP extracted to=${to.slice(0, 40)} subject="${subject.slice(0, 60)}"`);
    }

    // Optional: forward raw email to fallback mailbox (e.g. existing QQ inbox)
    // Useful during the IMAP→KV migration to keep both paths warm.
    if (env.FALLBACK_TO) {
      try {
        await message.forward(env.FALLBACK_TO);
      } catch (e) {
        console.error('forward failed:', e && e.message);
      }
    }
  },
};
