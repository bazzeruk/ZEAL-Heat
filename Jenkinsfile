// Jenkins equivalent of .github/workflows/tests.yml: the same two Home
// Assistant test environments, each in a clean Python container.
pipeline {
    agent none
    options {
        timeout(time: 30, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '20'))
    }
    stages {
        stage('Tests') {
            parallel {
                // Original pinned baseline.
                stage('HA 2025.1.4') {
                    agent { docker { image 'python:3.12' } }
                    environment { REQS = 'requirements_test.txt' }
                    steps { sh './ci/run_tests.sh' }
                    post { always { junit 'test-results.xml' } }
                }
                // Newest available test harness.
                stage('HA 2026.2.3') {
                    agent { docker { image 'python:3.13' } }
                    environment { REQS = 'requirements_test_latest.txt' }
                    steps { sh './ci/run_tests.sh' }
                    post { always { junit 'test-results.xml' } }
                }
            }
        }
    }
}
