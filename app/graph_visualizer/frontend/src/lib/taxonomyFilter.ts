import { Node } from '../types/graph';

/**
 * Check if a node matches the taxonomy keyword filter.
 * Matches if ANY taxonomy path contains the keyword (case-insensitive).
 */
export const matchesTaxonomyKeyword = (node: Node, keyword: string): boolean => {
  if (!keyword.trim()) return true;
  if (!node.taxonomy_paths || node.taxonomy_paths.length === 0) return false;

  const lowerKeyword = keyword.toLowerCase().trim();
  
  return node.taxonomy_paths.some(path => 
    path.toLowerCase().includes(lowerKeyword)
  );
};

/**
 * Check if a node matches the taxonomy path filter.
 * Ordered partial match: each segment must appear in order, but can have other segments between.
 * 
 * Example: "robot > battlebot" matches:
 * - "entity > artifact > machine > robot > competition_robot > battlebot" ✓
 * - "competition_robot > battle_robot > battlebot" ✓
 * - "battlebot > robot" ✗ (wrong order)
 */
export const matchesTaxonomyPath = (node: Node, pathFilter: string): boolean => {
  if (!pathFilter.trim()) return true;
  if (!node.taxonomy_paths || node.taxonomy_paths.length === 0) return false;

  // Parse the filter into segments
  const filterSegments = pathFilter
    .split('>')
    .map(s => s.trim().toLowerCase())
    .filter(s => s.length > 0);

  if (filterSegments.length === 0) return true;

  // Check if ANY of the node's taxonomy paths matches
  return node.taxonomy_paths.some(path => {
    const pathSegments = path
      .split('>')
      .map(s => s.trim().toLowerCase());

    // Check if all filter segments appear in order
    let filterIndex = 0;
    
    for (const pathSegment of pathSegments) {
      if (filterIndex >= filterSegments.length) {
        // All filter segments have been matched
        return true;
      }
      
      // Check if current path segment contains the current filter segment
      if (pathSegment.includes(filterSegments[filterIndex])) {
        filterIndex++;
      }
    }

    // Return true if all filter segments were matched
    return filterIndex >= filterSegments.length;
  });
};

/**
 * Check if a node passes all taxonomy filters.
 */
export const passesTaxonomyFilters = (
  node: Node,
  taxonomyKeyword: string,
  taxonomyPath: string
): boolean => {
  return (
    matchesTaxonomyKeyword(node, taxonomyKeyword) &&
    matchesTaxonomyPath(node, taxonomyPath)
  );
};


