Windows VIPM fallback resolution now matches VIPC-normalized versions to indexed
OpenG packaging releases without relaxing explicit release pins. Skipping an
unavailable transitive dependency no longer hides a later explicit required
package request. Required packages must still resolve and pass target-inventory
verification. This resolver fix does not upgrade VIPM or establish legacy package
installation compatibility; a compatible vendor runtime and real Windows bake
remain necessary.
