Drop these files here to trigger e2e payment test:
- session_token.txt     - ChatGPT session token (Path A)
- challenge.json        - {challenge_id, client_id, ts} from gwa OTP (Path B)
- qris_code.png         - QRIS code to scan-pay (small amount test)
- snap_token.txt        - Direct Midtrans snap token

Place file → /loop wakes immediately and runs matching path.
