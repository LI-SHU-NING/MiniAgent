"""快速排序演示脚本。

实现内容：
1. quick_sort(nums)：经典递归快速排序（原地排序，取末位元素为枢轴，Lomuto 分区）；
2. quick_sort_simple(nums)：函数式写法（返回新列表，便于对照验证）；
3. generate_random_numbers(n, low, high)：生成 n 个随机整数；
4. self_test()：随机生成 5 个数，打印排序前后结果，并断言排序结果正确。

直接运行：python demo/quicksort.py
"""

from __future__ import annotations

import random
from typing import List


def quick_sort(nums: List[int], low: int = 0, high: int | None = None) -> List[int]:
    """原地快速排序，返回同一个列表对象（便于链式使用）。"""
    if high is None:
        high = len(nums) - 1

    if low < high:
        pivot_index = _partition(nums, low, high)
        quick_sort(nums, low, pivot_index - 1)
        quick_sort(nums, pivot_index + 1, high)
    return nums


def _partition(nums: List[int], low: int, high: int) -> int:
    """Lomuto 分区：以 nums[high] 为枢轴，把小于等于枢轴的元素放到左侧。"""
    pivot = nums[high]
    i = low - 1
    for j in range(low, high):
        if nums[j] <= pivot:
            i += 1
            nums[i], nums[j] = nums[j], nums[i]
    nums[i + 1], nums[high] = nums[high], nums[i + 1]
    return i + 1


def quick_sort_simple(nums: List[int]) -> List[int]:
    """函数式快速排序：以中间元素为枢轴，返回新的有序列表。"""
    if len(nums) <= 1:
        return list(nums)
    pivot = nums[len(nums) // 2]
    less = [x for x in nums if x < pivot]
    equal = [x for x in nums if x == pivot]
    greater = [x for x in nums if x > pivot]
    return quick_sort_simple(less) + equal + quick_sort_simple(greater)


def generate_random_numbers(n: int = 5, low: int = 1, high: int = 100) -> List[int]:
    """生成 n 个指定范围内的随机整数（允许重复）。"""
    return [random.randint(low, high) for _ in range(n)]


def self_test(n: int = 5, low: int = 1, high: int = 100) -> bool:
    """自测：生成 n 个随机数，排序并校验结果。"""
    origin = generate_random_numbers(n, low, high)
    print(f"排序前（{n} 个随机数）：{origin}")

    inplace = list(origin)
    quick_sort(inplace)
    print(f"排序后（原地快排）：  {inplace}")

    snapshot = list(origin)  # 记录调用前的原始列表，用于校验函数式版本不修改入参
    functional = quick_sort_simple(origin)
    print(f"排序后（函数式快排）：{functional}")

    expected = sorted(origin)
    assert inplace == expected, f"原地快排结果错误：{inplace} != {expected}"
    assert functional == expected, f"函数式快排结果错误：{functional} != {expected}"
    assert origin == snapshot, "函数式快排不应修改传入的列表"
    print("✓ 自测通过：两种实现结果均与 sorted() 一致，且未污染入参")
    return True


if __name__ == "__main__":
    print("=" * 48)
    print("快速排序随机数自测")
    print("=" * 48)
    self_test()
    print("=" * 48)
    print("边界用例补充校验：")
    for case in ([], [7], [3, 3, 1, 2], [5, 4, 3, 2, 1]):
        print(f"  {case} -> {quick_sort(list(case))}")
    print("=" * 48)
