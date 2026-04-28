import { useState, useCallback } from 'react';
import { SearchFilters } from '../types/graph';

export const useFilters = () => {
  const [searchQuery, setSearchQuery] = useState('');
  const [nodeTypeFilter, setNodeTypeFilter] = useState('');
  const [edgeTypeFilter, setEdgeTypeFilter] = useState('');
  const [taxonomyKeyword, setTaxonomyKeyword] = useState('');
  const [taxonomyPath, setTaxonomyPath] = useState('');

  const updateSearchQuery = useCallback((query: string) => {
    setSearchQuery(query);
  }, []);

  const updateNodeTypeFilter = useCallback((filter: string) => {
    setNodeTypeFilter(filter);
  }, []);

  const updateEdgeTypeFilter = useCallback((filter: string) => {
    setEdgeTypeFilter(filter);
  }, []);

  const updateTaxonomyKeyword = useCallback((keyword: string) => {
    setTaxonomyKeyword(keyword);
  }, []);

  const updateTaxonomyPath = useCallback((path: string) => {
    setTaxonomyPath(path);
  }, []);

  const updateFilters = useCallback((filters: SearchFilters) => {
    setSearchQuery(filters.searchQuery);
    setNodeTypeFilter(filters.nodeTypeFilter);
    setEdgeTypeFilter(filters.edgeTypeFilter);
    setTaxonomyKeyword(filters.taxonomyKeyword);
    setTaxonomyPath(filters.taxonomyPath);
  }, []);

  const clearFilters = useCallback(() => {
    setSearchQuery('');
    setNodeTypeFilter('');
    setEdgeTypeFilter('');
    setTaxonomyKeyword('');
    setTaxonomyPath('');
  }, []);

  const getFilters = useCallback((): SearchFilters => ({
    searchQuery,
    nodeTypeFilter,
    edgeTypeFilter,
    taxonomyKeyword,
    taxonomyPath
  }), [searchQuery, nodeTypeFilter, edgeTypeFilter, taxonomyKeyword, taxonomyPath]);

  return {
    searchQuery,
    nodeTypeFilter,
    edgeTypeFilter,
    taxonomyKeyword,
    taxonomyPath,
    updateSearchQuery,
    updateNodeTypeFilter,
    updateEdgeTypeFilter,
    updateTaxonomyKeyword,
    updateTaxonomyPath,
    updateFilters,
    clearFilters,
    getFilters
  };
};
