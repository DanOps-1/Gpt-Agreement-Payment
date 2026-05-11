#!/usr/bin/env python3
"""诊断脚本：列出 KV 中所有的 OTP 记录"""
import sys
sys.path.insert(0, 'CTF-reg')

from cf_kv_otp_provider import CloudflareKVOtpProvider
import json

def list_all_keys():
    """列出 KV namespace 中的所有 key"""
    provider = CloudflareKVOtpProvider.from_env_or_secrets()

    # 使用 CF API 列出所有 keys
    path = f"/accounts/{provider.account_id}/storage/kv/namespaces/{provider.kv_id}/keys"
    try:
        result = provider._req("GET", path)
        if result and result.get("success"):
            keys = result.get("result", [])
            print(f"找到 {len(keys)} 个 key:")
            for item in keys:
                key_name = item.get("name", "")
                print(f"\n  Key: {key_name}")

                # 读取每个 key 的值
                try:
                    value = provider._kv_get(key_name)
                    if value:
                        print(f"    OTP: {value.get('otp', 'N/A')}")
                        print(f"    Timestamp: {value.get('ts', 'N/A')}")
                        print(f"    From: {value.get('from', 'N/A')[:60]}")
                        print(f"    Subject: {value.get('subject', 'N/A')[:60]}")
                except Exception as e:
                    print(f"    读取失败: {e}")
        else:
            print(f"API 返回失败: {result}")
    except Exception as e:
        print(f"列出 keys 失败: {e}")

if __name__ == "__main__":
    list_all_keys()
