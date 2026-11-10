// SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

#include "counter.h"

Counter::Counter(QObject *parent) : QObject(parent) {}

void Counter::setValue(int value) {
  if (value == value_)
    return;
  value_ = value;
  emit valueChanged(value);
}
