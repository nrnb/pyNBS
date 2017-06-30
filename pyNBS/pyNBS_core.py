############################################
# ---------- Core NBS Functions ---------- #
############################################

import random
import networkx as nx
import pandas as pd
import scipy.stats as stats
import numpy as np
import numpy.matlib as matlib
from scipy.optimize import nnls
from multiprocessing import Pool
import time

# Function to construct the KNN regularization network graph laplacian
# network is a NetworkX object
# gamma is the Vandin 2011 diagonal correction value (should be small)
# kn is the number of nearest neighbors to construct network regularizer from
def network_inf_KNN_glap(network, gamma=0.01, kn=11, verbose=False, save_path=None):
    glap_inv_starttime = time.time()
    # Construct network laplacian matrix
    network_nodes = network.nodes()
    L_arr = nx.laplacian_matrix(network).todense()
    # Adjust diagonal of laplacian matrix by small gamma as seen in Vandin 2011
    L_vandin = L_arr + gamma*np.identity(len(network_nodes))
    # Calculate the inverse of diagonal adjusted graph laplacian to get graph influence matrix (re: Vandin 2011)
    # This is significantly faster than the method proposed previously in NBS v0.2.0 to calculate the pseudoinverse
    # of each network component. The graph result may be slightly different, and thus the resulting clustering results.
    # But our analysis suggest that the results are not affected greatly (via OV on HM90 task.)
    L_inv_arr = np.linalg.inv(L_vandin)
    L_inv = pd.DataFrame(L_inv_arr, index = network_nodes, columns = network_nodes)
    if verbose:
        print 'Graph influence matrix calculated:', time.time()-glap_inv_starttime, 'seconds'
    KNN_starttime = time.time()
    # Construct KNN graph using the 11 nearest neighbors by influence score (glap_pinv)
    # The code may include each gene's self as the 'nearest' neighbor
    # Therefore the resulting laplacian maybe more like KNN with k=10 with self-edges
    # We only draw edges where the influence is > 0 between nodes
    KNN_graph = nx.Graph()
    for gene in L_inv.index:
        gene_knn = L_inv.ix[gene].sort_values(ascending=False)[:kn].index
        for neighbor in gene_knn:
            if L_inv.ix[gene][neighbor] > 0:
                KNN_graph.add_edge(gene, neighbor)
    KNN_nodes = KNN_graph.nodes()
    # Calculate KNN graph laplacian
    knnGlap_sparse = nx.laplacian_matrix(KNN_graph)
    knnGlap = pd.DataFrame(knnGlap_sparse.todense(), index=KNN_nodes, columns=KNN_nodes)
    if save_path is not None:
        knnGlap.to_hdf(save_path, key='knnGLap', mode='w')     
    if verbose:
        print 'Graph laplacian of KNN network from influence matrix calculated:', time.time()-KNN_starttime, 'seconds'    
    return knnGlap

# Function to sub-sample binary somatic mutation profile data frame in context of a given network
# If no network (propNet) is given, all genes are sub-sampled
# Key is that filtering for min mutation count happens before filtering by network nodes not after
def subsample_sm_mat(sm_mat, propNet=None, pats_subsample_p=0.8, gene_subsample_p=0.8, min_muts=10):
    # Number of indiv/features for sampling
    (Nind, Dfeat) = sm_mat.shape
    Nsample = round(Nind*pats_subsample_p)
    Dsample = round(Dfeat*gene_subsample_p)

    # Sub sample patients
    pats_subsample = random.sample(sm_mat.index, int(Nsample))
    # Sub sample genes
    gene_subsample = random.sample(sm_mat.columns, int(Dsample))
    # Sub sampled data mat
    gind_sample = sm_mat.ix[pats_subsample][gene_subsample]
    # Filter by mutation count
    gind_sample = gind_sample[gind_sample.sum(axis=1) > min_muts]
    if propNet is not None:
        # Filter columns by network nodes only (if network is given)
        gind_sample_filt = gind_sample.T.ix[propNet.nodes()].fillna(0).T
        return gind_sample_filt
    else:
        return gind_sample

# Function to quantile normalize a pandas DataFrame
# Code taken from: https://github.com/ShawnLYU/Quantile_Normalize/blob/master/quantile_norm.py
# Using implementation described on Wikipedia: https://en.wikipedia.org/wiki/Quantile_normalization
# data: Pandas DataFrame (propagated genotypes) where rows are samples (samples), and columns are features (genes)
# Returns df_out: Quantile normalized Pandas DataFrame with same orientation as data df
def qnorm(data):
    df = data.T
    df_out = df.copy(deep=True)
    dic = {}

    # Sort each patient by gene propagation value
    for col in df:
        dic.update({col:sorted(df[col])})
    sorted_df = pd.DataFrame(dic)

    # Rank averages for each gene across samples
    ranked_avgs = sorted_df.mean(axis = 1).tolist()

    # Construct quantile normalized Pandas DataFrame by assigning ranked averages to ranks of each gene for each sample
    for col in df_out:
        t = stats.rankdata(df[col]).astype(int)
        df_out[col] = [ranked_avgs[i-1] for i in t]
    return df_out.T

# Helper function for computing non-negative least squares solution
def nnls_single(solution_vect):
    return nnls(factor_matrix, solution_vect)[0]

# Adapted from Matan Hofree's Matlab code in NBS
# data = features-by-samples propagated (or not) mutation profiles
# KNN_glap = Graph laplacian of regularization network
# Note: Make sure that the rows of Y are aligned correctly with the rows and columns of KNN_glap before passing into function
# k = Number of clusters for factorization
# H_init = Optional initial H array (k-by-samples)
# W_init = Optional nitial W array (features-by-k)
# gamma = Network regularization term constant
# eps = Small number precision
# update_gamma = Whether or not to update gamma regularization term on the fly
# gamma_factor = Factor to scale new gamma regularization term by (based on residual value ratios)
# Loop break conditions:
#   err_tol = Maximum value of reconstruction error function allowed for break
#   err_delta_tol = Maximum change in reconstruction error allowed for break
#   niter = Maximum number of iterations to execute before break
# verbose = print statements on update progress
# debug_mode = Returns intermediate values during updating if desired   
def mixed_netNMF(data, KNN_glap, k, W_init=None, H_init=None, gamma=200, update_gamma=False, gamma_factor=1, 
                 niter=250, eps=1e-15, err_tol=1e-4, err_delta_tol=1e-4, verbose=True, debug_mode=False):
    # Initialize H and W Matrices from data array if not given
    r, c = data.shape[0], data.shape[1]
    # Initialize H
    if H_init is None:
        H_init = np.random.rand(k,c)
        H = np.maximum(H_init, eps)
    else:
        H = np.copy(H_init)
    # Initialize W
    if W_init is None:
        W_init = np.linalg.lstsq(H.T, data.T)[0].T
        W_init = np.dot(W_init, np.diag(1/sum(W_init)))
        W = np.maximum(W_init, eps)
    else:
        W = np.copy(W_init)
    global factor_matrix
    factor_matrix = W        
    if verbose:
        print 'W and H matrices initialized'
    
    # Get graph matrices from laplacian array
    D = np.diag(np.diag(KNN_glap)).astype(float)
    A = (D-KNN_glap).astype(float)
    if verbose:
        print 'D and A matrices calculated'
    # Set mixed netNMF reporting variables
    optGammaIterMin, optGammaIterMax = 0, niter/2
    if debug_mode:
        resVal, resVal_Kreg, fitResVect, fitGamma, Wlist, Hlist = [], [], [], [], [], []
    XfitPrevious = np.inf
    
    # Updating W and H
    for i in range(niter):
        XfitThis = np.dot(W, H)
        WHres = np.linalg.norm(data-XfitThis) # Reconstruction error

        # Change in reconstruction error
        if i == 0:
            fitRes = np.linalg.norm(XfitPrevious)
        else:
            fitRes = np.linalg.norm(XfitPrevious-XfitThis)
        XfitPrevious = XfitThis
        # Tracking reconstruction errors and residuals
        if debug_mode:
            resVal.append(WHres)
            fitResVect.append(fitRes)
            fitGamma.append(gamma)
            Wlist.append(W)
            Hlist.append(H)
        if (verbose) & (i%10==0):
            if update_gamma:
                print 'Iteration >>', i, 'Mat-res:', WHres, 'K-res:', Kres, 'Sum:', WHres+np.sqrt(Kres), 'Gamma:', gamma, 'Wfrob:', np.linalg.norm(W)
            else:
                print 'Iteration >>', i, 'Mat-res:', WHres, 'Gamma:', gamma, 'Wfrob:', np.linalg.norm(W)
        if (err_delta_tol > fitRes) | (err_tol > WHres) | (i+1 == niter):
            if verbose:
                print 'NMF completed!'
                print 'Total iterations:', i+1
                print 'Final Reconstruction Error:', WHres
                print 'Final Reconstruction Error Delta:', fitRes
            numIter = i+1
            finalResidual = WHres
            break

        # Update Gamma
        if (update_gamma==True) & (gamma_factor!=0) & (i+1 <= optGammaIterMax) & (i+1 > optGammaIterMin):
            # Un-scaled regularization term (originally sqrt of this value is taken)
            KWmat = np.dot(KNN_glap, W)
            Kres = np.trace(np.dot(W.T, KWmat)) 
            if debug_mode:
                resVal_Kreg.append(Kres)
            new_gamma = round((WHres/np.sqrt(Kres))*gamma_factor)
            gamma = new_gamma
        # Terms to be scaled by gamma
        KWmat_D = np.dot(D,W) 
        KWmat_W = np.dot(A,W)
            
        # Update W with network constraint
        W = W*((np.dot(data, H.T) + gamma*KWmat_W + eps) / (np.dot(W,np.dot(H,H.T)) + gamma*KWmat_D + eps))
        W = np.maximum(W, eps)
        W = W/matlib.repmat(np.maximum(sum(W),eps),len(W),1);        
        factor_matrix = W
        
        # Update H
        H = np.array([nnls(W, data[:,j])[0] for j in range(c)]).T 
        # ^ Matan uses a custom fast non-negative least squares solver here, we will use scipy's implementation here
        H=np.maximum(H,eps)
    
    if debug_mode:
        return W, H, numIter, finalResidual, resVal, resVal_Kreg, fitResVect, fitGamma, Wlist, Hlist
    else:
        return W, H, numIter, finalResidual