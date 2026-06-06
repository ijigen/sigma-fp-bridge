#!/usr/bin/env python3
"""Sigma fp 診斷工具 — 動態 dump 所有 attribute"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from sigma_fp_focus import open_camera, close_camera, get_focus_state


def dump(name, obj):
    print(f"\n--- {name} ({type(obj).__name__}) ---")
    if obj is None:
        print("  (None)")
        return
    for attr in sorted(dir(obj)):
        if attr.startswith('_'):
            continue
        try:
            val = getattr(obj, attr)
        except Exception as e:
            val = f"<error: {e}>"
            continue
        if callable(val):
            continue
        print(f"  {attr} = {val!r}")


def main():
    print("=" * 70)
    print("Sigma fp Diagnose")
    print("=" * 70)

    cam = open_camera()
    try:
        method_names = [
            ("DataGroup1", "get_cam_data_group1"),
            ("DataGroup2", "get_cam_data_group2"),
            ("DataGroup3", "get_cam_data_group3"),
            ("DataGroup4", "get_cam_data_group4"),
            ("DataGroup5", "get_cam_data_group5"),
            ("DataGroupFocus", "get_cam_data_group_focus"),
            ("CanSetInfo5", "get_cam_can_set_info5"),
            ("CamStatus", "get_cam_status"),
            ("CamStatus2", "get_cam_status2"),
        ]
        for name, method_name in method_names:
            fn = getattr(cam, method_name, None)
            if fn is None:
                print(f"\n--- {name} 跳過：{method_name}() 不存在")
                continue
            try:
                obj = fn()
                dump(name, obj)
            except Exception as e:
                print(f"\n--- {name} 失敗：{type(e).__name__}: {e}")
    finally:
        close_camera(cam)
        print("\n關閉相機。")


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
