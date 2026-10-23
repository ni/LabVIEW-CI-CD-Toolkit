Caraya runs now retain runner startup/execution failures and LabVIEW CLI logs in
the Unit Tests report instead of hiding an empty result as a missing report.
The driver reads the configurator's nested configuration, preserves command
quoting, removes stale output, and fails when an enabled runner cannot execute.
Invalid JUnit, zero tests, and all-skipped runs cannot produce a green result.

Caraya 1.4.4.148 is installed with the required early worker packages; a failed
required bake now explicitly fails the final worker Docker build. Rebuild
existing seed-only workers and supply all project dependencies. The documented
native LabVIEWCLI wrapper path passed 44 assertions locally in Measurement
Utility Reference; clean-container validation is not yet established. The old
g-cli extension command remains an unverified scaffold.