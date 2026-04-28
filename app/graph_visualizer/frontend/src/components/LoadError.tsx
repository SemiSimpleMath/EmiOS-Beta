import React from 'react';

interface LoadErrorProps {
  error: string;
  retryCount: number;
  onRetry: () => void;
}

const LoadError: React.FC<LoadErrorProps> = ({ error, retryCount, onRetry }) => {
  return (
    <div className="bg-red-50 border border-red-200 rounded-md p-4 m-4">
      <div className="flex items-center">
        <div className="flex-shrink-0">
          <svg className="h-5 w-5 text-red-400" viewBox="0 0 20 20" fill="currentColor">
            <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clipRule="evenodd" />
          </svg>
        </div>
        <div className="ml-3 flex-1">
          <h3 className="text-sm font-medium text-red-800">
            Failed to load graph data
          </h3>
          <div className="mt-2 text-sm text-red-700">
            <p>{error}</p>
            {retryCount > 0 && (
              <p className="mt-1">Retry attempts: {retryCount}/3</p>
            )}
          </div>
          <div className="mt-4">
            <button
              onClick={onRetry}
              className="bg-red-100 px-3 py-2 rounded-md text-sm font-medium text-red-800 hover:bg-red-200 focus:outline-none focus:ring-2 focus:ring-red-500"
            >
              🔄 Retry
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};

export default LoadError;
