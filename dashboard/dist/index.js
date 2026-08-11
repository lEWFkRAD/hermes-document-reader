(function () {
  'use strict'
  if (!window.__HERMES_PLUGINS__ || !window.__HERMES_PLUGIN_SDK__) return
  var React = window.__HERMES_PLUGIN_SDK__.React
  function HiddenDocumentReaderApi() { return React.createElement(React.Fragment, null) }
  window.__HERMES_PLUGINS__.register('document-reader', HiddenDocumentReaderApi)
})()
