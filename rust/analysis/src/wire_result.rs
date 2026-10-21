//! Keep normalized curve points typed until the final JSON serialization.

use serde::ser::SerializeMap;
use serde::{Serialize, Serializer};
use serde_json::Value;

use crate::result_reader::Point;

pub struct WireResult {
    pub(crate) value: Value,
    pub(crate) points: Option<Vec<Point>>,
    pub(crate) performance_only: bool,
}

struct Performance<'a> {
    value: &'a Value,
    points: &'a [Point],
}

impl Serialize for Performance<'_> {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let object = self.value.as_object().expect("performance is an object");
        let mut map = serializer.serialize_map(Some(object.len()))?;
        for (key, value) in object {
            if key == "points" {
                map.serialize_entry(key, self.points)?;
            } else {
                map.serialize_entry(key, value)?;
            }
        }
        map.end()
    }
}

impl Serialize for WireResult {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let Some(points) = self.points.as_deref() else {
            return self.value.serialize(serializer);
        };
        if self.performance_only {
            return Performance {
                value: &self.value,
                points,
            }
            .serialize(serializer);
        }
        let object = self.value.as_object().expect("snapshot is an object");
        let mut map = serializer.serialize_map(Some(object.len()))?;
        for (key, value) in object {
            if key == "result" {
                map.serialize_entry(key, &Performance { value, points })?;
            } else {
                map.serialize_entry(key, value)?;
            }
        }
        map.end()
    }
}
