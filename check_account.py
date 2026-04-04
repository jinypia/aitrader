#!/usr/bin/env python3
"""
Account Settings Checker
Shows all account-related configuration from .env file and scheduler settings
"""

import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

def check_account_settings():
    print("💰 ACCOUNT SETTINGS CHECK")
    print("=" * 50)

    # Account Information
    print("\n🏦 Account Configuration:")
    account_no = os.getenv('ACCOUNT_NO', 'Not set')
    print(f"   Account Number: {account_no}")

    # API Endpoints
    print("\n🔗 Account API Endpoints:")
    api_path = os.getenv('ACCOUNT_API_PATH', 'Not set')
    lookup_id = os.getenv('ACCOUNT_LOOKUP_API_ID', 'Not set')
    cash_id = os.getenv('ACCOUNT_CASH_API_ID', 'Not set')
    holdings_id = os.getenv('ACCOUNT_HOLDINGS_API_ID', 'Not set')
    query_type = os.getenv('ACCOUNT_QUERY_TYPE', 'Not set')

    print(f"   API Path: {api_path}")
    print(f"   Lookup API ID: {lookup_id}")
    print(f"   Cash API ID: {cash_id}")
    print(f"   Holdings API ID: {holdings_id}")
    print(f"   Query Type: {query_type}")

    # Refresh Settings
    refresh_sec = os.getenv('ACCOUNT_REFRESH_SEC', 'Not set')
    print(f"   Refresh Interval: {refresh_sec} seconds")

    # Trading Limits
    print("\n📊 Trading Limits:")
    max_orders = os.getenv('MAX_DAILY_ORDERS', 'Not set')
    position_size = os.getenv('POSITION_SIZE', 'Not set')
    initial_cash = os.getenv('INITIAL_CASH', 'Not set')

    print(f"   Max Daily Orders: {max_orders}")
    print(f"   Position Size: {position_size}")
    print(f"   Initial Cash: ₩{int(initial_cash):,} KRW" if initial_cash != 'Not set' else "   Initial Cash: Not set")

    # Risk Settings
    print("\n⚠️  Risk Management:")
    dry_run = os.getenv('DRY_RUN', 'Not set')
    print(f"   Dry Run Mode: {dry_run}")

    # Scalping Scheduler Settings
    print("\n🚀 Scalping Scheduler Account Settings:")
    print("   Note: Scheduler uses simplified risk management")

    # Check if all required settings are present
    print("\n✅ Configuration Status:")

    required_settings = [
        ('ACCOUNT_NO', account_no),
        ('ACCOUNT_API_PATH', api_path),
        ('ACCOUNT_LOOKUP_API_ID', lookup_id),
        ('ACCOUNT_CASH_API_ID', cash_id),
        ('ACCOUNT_HOLDINGS_API_ID', holdings_id),
        ('ACCOUNT_QUERY_TYPE', query_type),
        ('ACCOUNT_REFRESH_SEC', refresh_sec),
        ('MAX_DAILY_ORDERS', max_orders),
        ('POSITION_SIZE', position_size),
        ('INITIAL_CASH', initial_cash),
        ('DRY_RUN', dry_run),
    ]

    all_set = True
    for setting_name, value in required_settings:
        if value == 'Not set':
            print(f"   ❌ {setting_name}: Missing")
            all_set = False
        else:
            print(f"   ✅ {setting_name}: Configured")

    print("\n" + "=" * 50)
    if all_set:
        print("🎉 All account settings are properly configured!")
    else:
        print("⚠️  Some account settings are missing. Please check your .env file.")

    return all_set

if __name__ == "__main__":
    check_account_settings()