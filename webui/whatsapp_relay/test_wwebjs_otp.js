const assert = require("assert");

process.env.NODE_ENV = "test";
const relay = require("./index.js");

const msg = relay.wwebjsMessageToSidecar({
  body: "",
  from: "gopay@c.us",
  author: "",
  type: "chat",
  id: { _serialized: "MSG1" },
  rawData: {
    body: "",
    quotedMsg: {
      body: "Kode verifikasi GoPay Anda 445566. Jangan bagikan kode ini.",
    },
  },
});

assert.match(msg.body, /445566/);
relay.handleMessage(msg, "wwebjs:test");

const state = relay.readState();
assert.equal(state.latest.otp, "445566");
assert.equal(state.latest.engine, "wwebjs");
assert.equal(state.latest.from, "gopay@c.us");
assert.equal(state.history.length, 1);
