#!/bin/bash

SCRIPT=$(realpath "$0")
SCRIPTDIR=$(dirname "${SCRIPT}")

(cd ${SCRIPTDIR} && ${SCRIPTDIR}/tools/bazelisk run -c opt //agents/test_balancer:bullet)