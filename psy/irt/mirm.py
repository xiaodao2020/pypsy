# coding=utf-8
import warnings
from itertools import combinations
import numpy as np
from psy.fa import GPForth, Factor
from psy.irt.base import BaseEmIrt, MirtLogit2PLMixin
from psy.utils import get_nodes_weights, inverse_logistic


class Mirt(BaseEmIrt, MirtLogit2PLMixin):
    # 多维项目反应理论（全息项目因子分析）参数估计
    def __init__(self, dim_size, init_slop=None, init_threshold=None, max_iter=1000, tol=1e-4, *args, **kwargs):
        super(Mirt, self).__init__(*args, **kwargs)
        self._dim_size = dim_size
        if init_slop is not None and init_threshold is not  None:
            self._init_slop = init_slop.copy()
            self._init_threshold = init_threshold.copy()
        else:
            self._init_slop, self._init_threshold = self._get_init_slop_threshold(dim_size)
        self._fix_slop(dim_size)
        self._max_iter = max_iter
        self._tol = tol
        self._theta_size = self.scores.shape[0]
        self._theta_comb = self._get_theta_comb()
        self._nodes, self._weights = get_nodes_weights(dim_size)

    def _fix_slop(self, dim_size):
        # 由于多维参数的解空间无数，需要固定参数
        temp_idx = dim_size - 1
        while temp_idx:
            self._init_slop[temp_idx][-temp_idx:] = 0
            temp_idx -= 1

    def _get_theta_comb(self):
        # 多维特质的两两组合分布，这个主要目的是求二阶导用
        return list(combinations(range(self._dim_size), 2))

    def _get_theta_mat(self, theta):
        # theta的矩阵，包括0次方，1次方，二次方和交互，目的是矩阵乘法求海塞矩阵，避免for循环
        col1 = np.ones((theta.shape[0], 1))
        col2 = theta
        col3 = theta ** 2
        mat = np.zeros((theta.shape[0], len(self._theta_comb)))
        for i, v in enumerate(self._theta_comb):
            mat[:, i] = theta[:, v[0]] * theta[:, v[1]]
        return np.concatenate((col1, col2, col3, mat), axis=1)

    def _m_step(self, full_dis, right_dis, slop, threshold, theta, p_val):
        dp = right_dis - full_dis * p_val
        ddp = full_dis * p_val * (1 - p_val)
        jac1 = np.sum(dp, axis=0)
        jac1.shape = 1, jac1.shape[0]
        jac2 = np.dot(theta.transpose(), dp)
        jac_all = np.vstack((jac1, jac2))
        base_hess = self._get_theta_mat(theta)
        # 海塞矩阵的数值矩阵，不是海塞矩阵
        fake_hess = -1 * np.dot(ddp.transpose(), base_hess)
        slop_delta_list = np.zeros_like(slop)
        threshold_delta_list = np.zeros_like(threshold)
        i = slop.shape[1] - 1
        # 固定参数的初始位置
        fix_param_size = self._dim_size - 1
        # 把求稀疏矩阵的逆转化成求每个题目的小矩阵的逆
        while i >= 0:
            # jac矩阵
            jac = self._get_jac(jac_all, i, fix_param_size)
            # 海塞矩阵
            hess = self._get_hess(fake_hess, i, fix_param_size)
            delta = np.linalg.solve(hess, jac)
            slop_est_param_idx = self._dim_size - fix_param_size
            slop[:slop_est_param_idx, i] = slop[:slop_est_param_idx, i] - delta[1:]
            threshold[:, i] = threshold[:, i] - delta[0]
            slop_delta_list[:slop_est_param_idx, i] = delta[1:]
            threshold_delta_list[:, i] = delta[0]
            i -= 1
            if fix_param_size > 0:
                fix_param_size -= 1
        return slop, threshold, slop_delta_list, threshold_delta_list

    def _get_jac(self, jac_all, i, fix_param_size):
        return jac_all[:, i][:self._dim_size - fix_param_size + 1]

    def _get_hess(self, hess_vals, i, fix_param_size):
        # 求海赛矩阵
        param_size = self._dim_size + 1 - fix_param_size
        hess = np.zeros((param_size, param_size))
        hess[0] = hess_vals[i][:param_size]
        hess[1:, 0] = hess_vals[i][1:param_size]
        for j in range(1, param_size):
            hess[j, j] = hess_vals[i][self._dim_size + j]
        for k, comb in enumerate(self._theta_comb):
            try:
                val = hess_vals[i][self._dim_size + 1 + self._dim_size + k]
                hess[comb[0] + 1, comb[1] + 1] = val
                hess[comb[1] + 1, comb[0] + 1] = val
            except IndexError:
                break
        return hess

    def fit(self):
        max_iter = self._max_iter
        tol = self._tol
        slop = self._init_slop
        threshold = self._init_threshold
        for i in range(max_iter):
            z = self.z(slop, threshold, self._nodes)
            p_val = self.p(z)
            full_dis, right_dis, loglik = self._e_step(p_val, self._weights)
            slop, threshold, slop_delta_list, threshold_delta_list = self._m_step(full_dis, right_dis, slop, threshold, self._nodes, p_val)
            if np.max(np.abs(slop_delta_list)) < tol and np.max(np.abs(threshold_delta_list)) < tol:
                print(i)
                return slop, threshold, self._get_factor_loadings(slop)
        warnings.warn("no convergence, the smallest delta is %s" %
                      max(np.max(np.abs(slop_delta_list)), np.max(np.abs(threshold_delta_list))))
        return slop, threshold, self._get_factor_loadings(slop)

    @staticmethod
    def _get_factor_loadings(slop):
        # 将求得解转化为因子载荷
        d = (1 + np.sum((slop / 1.702) ** 2, axis=0)) ** 0.5
        d.shape = 1, d.shape[0]
        init_loadings = slop / (d * 1.702)
        loadings = GPForth(init_loadings.transpose()).solve()
        return loadings

    def _get_init_slop_threshold(self, dim_size):
        # 求初始值
        # 斜率是因子分析后的因子载荷转化
        # 阈值是logistic函数的反函数转化
        loadings = Factor(self.scores, dim_size).mirt_loading
        loadings_tr = loadings.transpose()
        d = (1 - np.sum(loadings_tr ** 2, axis=0)) ** 0.5
        init_slop = loadings_tr / d * 1.702
        init_threshold = inverse_logistic(np.mean(self.scores, axis=0))
        init_threshold.shape = 1, init_threshold.shape[0]
        init_threshold = init_threshold / d
        return init_slop, init_threshold