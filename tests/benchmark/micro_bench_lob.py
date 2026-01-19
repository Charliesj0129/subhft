import timeit

import numpy as np

# Setup
bids = np.array([[10000, 1], [9950, 2], [9900, 3], [9850, 4], [9800, 5]], dtype=np.int64)


def numpy_sum(arr):
    if arr.size > 0:
        return np.sum(arr[:, 1])
    return 0


def python_sum_from_numpy(arr):
    # Iterating numpy array in python is usually slow
    s = 0
    for i in range(arr.shape[0]):
        s += arr[i, 1]
    return s


def pure_python_data_sum(data_list):
    # If we stored as list of lists instead of numpy
    s = 0
    for row in data_list:
        s += row[1]
    return s


if __name__ == "__main__":
    t_np = timeit.timeit(lambda: numpy_sum(bids), number=100000)
    t_py_np = timeit.timeit(lambda: python_sum_from_numpy(bids), number=100000)

    # Simulate if we kept data as lists
    bids_list = [[10000, 1], [9950, 2], [9900, 3], [9850, 4], [9800, 5]]
    t_list = timeit.timeit(lambda: pure_python_data_sum(bids_list), number=100000)

    print(f"Numpy Sum: {t_np:.4f}s")
    print(f"Py Iter Numpy: {t_py_np:.4f}s")
    print(f"Pure List Sum: {t_list:.4f}s")
